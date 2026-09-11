// The delegation loop as a sidebar: the Boss at the top, and under it one
// clickable bar per session it delegated to. Each bar names the worker's
// task, shows a loading indicator while it works, says that it reports
// back to the Boss, and jumps to that workspace on click. Every bar is a
// real Button with a trailing chevron so it reads as clickable. Managed
// workspaces are recognised by the description stamp (see
// cmux_runtime.MANAGED_MARK); everything else lists below, untouched.
//
// Installed by the app into ~/.config/cmux/sidebars/ (cmux_setup
// .install_sidebar). Interpreted by cmux's custom-sidebar runtime:
// a single SwiftUI-style view expression, hot-reloaded on save.
VStack(alignment: .leading, spacing: 8) {
    Text("Conductor").font(.headline)
    Divider()
    ForEach(workspaces.filter { $0.description.hasPrefix("conductor-managed agent session: cond_boss") }.prefix(1)) { boss in
        VStack(alignment: .leading, spacing: 6) {
            Button(action: { cmux("workspace.select", workspace_id: boss.id) }) {
                HStack(spacing: 8) {
                    Circle().fill("#B57BDF").frame(width: 9, height: 9)
                    Text("Boss").font(.system(size: 13)).bold()
                    Spacer()
                    Image(systemName: "chevron.right").foregroundColor(.secondary).imageScale(.small)
                }
                .padding(8)
                .background(boss.selected ? "#B57BDF33" : "#B57BDF14")
                .cornerRadius(8)
            }
            ForEach(workspaces.filter { $0.description.hasPrefix("conductor-managed agent session: cond_task") }) { w in
                Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
                    HStack(spacing: 8) {
                        Image(systemName: "arrow.turn.down.right").foregroundColor(.secondary).imageScale(.small)
                        if let c = w.color {
                            Circle().fill("\(c)").frame(width: 8, height: 8)
                        } else {
                            Circle().fill("teal").frame(width: 8, height: 8)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(w.title).font(.system(size: 12)).lineLimit(1)
                            if let p = w.progress {
                                if p.label.hasPrefix("waiting") {
                                    Text(p.label).font(.caption).foregroundColor(.orange)
                                } else {
                                    Text(p.label).font(.caption).foregroundColor(.secondary)
                                }
                            } else {
                                Text("reports back to Boss").font(.caption).foregroundColor(.secondary)
                            }
                        }
                        Spacer()
                        if let p = w.progress {
                            if p.label.hasPrefix("working") {
                                ProgressView().scaleEffect(0.5)
                            } else if p.label.hasPrefix("waiting") {
                                Image(systemName: "questionmark.circle.fill").foregroundColor(.orange)
                            } else if p.label.hasPrefix("failed") {
                                Image(systemName: "xmark.circle.fill").foregroundColor(.red)
                            } else {
                                Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
                            }
                        }
                        Image(systemName: "chevron.right").foregroundColor(.secondary).imageScale(.small)
                    }
                    .padding(8)
                    .background(w.selected ? "#7F7F7F2E" : "#7F7F7F14")
                    .cornerRadius(8)
                }
                .padding(.leading, 12)
            }
        }
    }
    Divider()
    Text("Workspaces").font(.caption).foregroundColor(.secondary)
    ForEach(workspaces.filter { !$0.description.hasPrefix("conductor-managed agent session") }.prefix(20)) { w in
        Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
            HStack(spacing: 8) {
                Circle().fill(w.selected ? "accent" : "clear").frame(width: 7, height: 7)
                Text(w.title).font(.system(size: 12)).lineLimit(1)
                Spacer()
                Image(systemName: "chevron.right").foregroundColor(.secondary).imageScale(.small)
            }
            .padding(6)
            .background(w.selected ? "#7F7F7F1E" : "#00000000")
            .cornerRadius(6)
        }
    }
    Spacer()
}
.padding(8)
