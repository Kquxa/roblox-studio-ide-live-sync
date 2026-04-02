roblox studio ide live sync is a lightweight bridge between Roblox Studio and your local IDE that keeps Luau scripts synced both ways in real time. Instead of manually exporting from Studio every time you change a script, this tool automatically sends Studio edits to your project folder and pulls file changes from your IDE back into Studio.

The project includes a Python server and a Roblox Studio plugin. The Python server watches your local script folder, handles incoming updates from Studio, and exposes change events for the plugin. The plugin monitors Script, LocalScript, and ModuleScript instances inside Studio, pushes edits to disk, and imports file updates back into open Studio scripts. It also uses Roblox’s script editor APIs so changes from your IDE appear properly in Studio without needing to reopen the script tab.

This makes it much easier to work with Roblox code using normal development tools like VS Code, Codex, Git, and GitHub. It is especially useful for solo developers or small teams who want a smoother workflow between Studio and a real code editor, without relying on constant manual exporting and importing.

Current features include:

automatic Studio -> folder sync
automatic folder -> Studio sync
local HTTP bridge server
change polling/event feed for live updates
fallback manual export/import buttons
basic loop prevention to avoid sync bouncing
optional filesystem event support through Python watchdog
The goal of the project is to make Roblox scripting feel closer to a normal software workflow: edit in Studio or edit in your IDE, and your code stays connected. It is designed as a local-first solution that can later be paired with GitHub for collaboration and version control.

This project currently focuses on script source synchronization which allows to use codex or other AI plugins for ur roblox studio projects. 
