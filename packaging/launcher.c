/* The app's executable: CPython, embedded, with its home inside the bundle.
 *
 * Why a compiled launcher and not a shell script or a copied `python3`:
 * macOS decides which app a process is from the path of the executable
 * the kernel started. A script's executable is /bin/bash; a copied
 * interpreter finds its prefix next to itself, outside Contents/MacOS.
 * This binary IS Contents/MacOS/<name>, so NSBundle.mainBundle() is the
 * app, the Dock tile and menu bar take its Info.plist name and icon, and
 * the privacy grants (Microphone, Input Monitoring, Accessibility, Screen
 * Recording) are asked for and held by the app rather than by "Python".
 *
 * It behaves like `python3`, with one default:
 *
 *   <app>                          -> python -m conductor.app_launch
 *   <app> --home X                 -> python -m conductor.app_launch --home X
 *   <app> script.py args / -c / -m -> python script.py args ...
 *
 * so the app's own child processes (the overlay, the Boss window, the
 * computer-use CLI a worker runs) are started as this same executable.
 *
 * Nothing is taken from the environment: no PYTHONHOME, PYTHONPATH or
 * user site-packages can reach in, and nothing is put into it, so a
 * worker's own `python3` never inherits this interpreter's paths.
 */
#include <Python.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void die(const char *what) {
    fprintf(stderr, "launcher: %s\n", what);
    exit(70);
}

/* dirname, n times, in place. */
static void up(char *path, int n) {
    while (n-- > 0) {
        char *slash = strrchr(path, '/');
        if (slash == NULL || slash == path)
            die("executable path has no bundle around it");
        *slash = '\0';
    }
}

/* Whether argv[1] asks for the interpreter rather than the app. */
static int wants_python(const char *arg) {
    size_t n = strlen(arg);
    if (strcmp(arg, "-c") == 0 || strcmp(arg, "-m") == 0 ||
        strcmp(arg, "-u") == 0 || strcmp(arg, "-I") == 0 ||
        strcmp(arg, "-") == 0 || strcmp(arg, "-V") == 0 ||
        strcmp(arg, "--version") == 0)
        return 1;
    return n > 3 && strcmp(arg + n - 3, ".py") == 0;
}

int main(int argc, char **argv) {
    char raw[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0)
        die("executable path too long");
    char exe[PATH_MAX];
    if (realpath(raw, exe) == NULL)
        die("cannot resolve the executable path");

    char resources[PATH_MAX];
    strlcpy(resources, exe, sizeof resources);
    up(resources, 2);                       /* .../Contents */
    strlcat(resources, "/Resources", sizeof resources);
    char home[PATH_MAX], app[PATH_MAX];
    snprintf(home, sizeof home, "%s/python", resources);
    snprintf(app, sizeof app, "%s/app", resources);

    /* Finder once passed -psn_0_NNN; it means nothing to us. */
    int kept = 0;
    char **args = calloc((size_t)argc + 3, sizeof *args);
    if (args == NULL)
        die("out of memory");
    args[kept++] = exe;
    int python_mode = argc > 1 && wants_python(argv[1]);
    if (!python_mode) {
        args[kept++] = "-m";
        args[kept++] = "conductor.app_launch";
    }
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) == 0)
            continue;
        args[kept++] = argv[i];
    }

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.use_environment = 0;
    config.user_site_directory = 0;
    config.write_bytecode = 0;     /* a signed bundle is never written to */
    config.safe_path = 0;          /* a script's own directory is importable */
    config.parse_argv = 1;

#define CHECK(s) do { status = (s); if (PyStatus_Exception(status)) goto fail; } while (0)
    CHECK(PyConfig_SetBytesString(&config, &config.home, home));
    CHECK(PyConfig_SetBytesString(&config, &config.executable, exe));
    CHECK(PyConfig_SetBytesString(&config, &config.program_name, exe));
    CHECK(PyConfig_SetBytesArgv(&config, kept, args));
    CHECK(Py_InitializeFromConfig(&config));
    PyConfig_Clear(&config);

    /* The app's own code: `import boss` and `-m conductor.x` work from any
     * cwd, for the app and for every child started as this executable.
     * (config.pythonpath_env is PYTHONPATH, and ignored with the
     * environment.) Py_RunMain puts the script's directory in front. */
    PyObject *path = PySys_GetObject("path");
    PyObject *entry = PyUnicode_DecodeFSDefault(app);
    if (path == NULL || entry == NULL || PyList_Insert(path, 0, entry) != 0)
        die("cannot put the app's code on sys.path");
    Py_DECREF(entry);
    return Py_RunMain();

fail:
    PyConfig_Clear(&config);
    if (PyStatus_IsExit(status))
        return status.exitcode;
    Py_ExitStatusException(status);
}
