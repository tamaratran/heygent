-- Lay out the mounted disk image's Finder window for release.sh:
-- icon view, no toolbar or sidebar, the background from .background,
-- heygent on the left and Applications on the right, at the slots the
-- background's arrow points between (scripts/dmg_background.py).
--
--     osascript scripts/dmg_layout.applescript <volume name>
on run argv
  set volumeName to item 1 of argv
  tell application "Finder"
    tell disk volumeName
      open
      set theWindow to container window
      set current view of theWindow to icon view
      set toolbar visible of theWindow to false
      set statusbar visible of theWindow to false
      set sidebar width of theWindow to 0
      -- window bounds: 600 x 400 content, matching the background
      set bounds of theWindow to {200, 120, 800, 520}
      set viewOptions to the icon view options of theWindow
      set arrangement of viewOptions to not arranged
      set icon size of viewOptions to 128
      set text size of viewOptions to 14
      set background picture of viewOptions to file ".background:background.png"
      set position of item "heygent.app" to {160, 190}
      set position of item "Applications" to {440, 190}
      close
      open
      update without registering applications
      delay 2
      close
    end tell
  end tell
end run
