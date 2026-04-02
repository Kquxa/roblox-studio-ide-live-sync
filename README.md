# Roblox Live Sync

Keep Roblox Studio and your local IDE folder synced both ways in real time.

This project uses:

- a Python server to watch your local `.luau` files
- a Roblox Studio plugin to sync script changes between Studio and disk

If you edit a script in Roblox Studio, it updates your local project files automatically.  
If you edit a synced file in your IDE and save it, the change is pushed back into Studio automatically.

## Requirements

- Windows
- Python 3.10 or newer
- Roblox Studio with plugin access enabled
- HTTP requests enabled in Studio

Optional:

- `watchdog` Python package for event-based file watching instead of polling

## Initial Setup

1. Download or clone this repository.
2. Open a terminal in the project folder.
3. Start the Python server:

```powershell
python export_server.py
```

4. Copy the contents of `roblox_exporter.plugin.lua` into a Roblox Studio plugin.
5. Make sure the Python server is running before using the plugin.

## Optional Installation

To enable more efficient filesystem watching:

```powershell
pip install watchdog
```

Without `watchdog`, the server still works by falling back to a 1 second polling loop.

## Project Structure

- `export_server.py` - Local sync server and file watcher
- `roblox_exporter.plugin.lua` - Roblox Studio plugin for live sync
- `MyGame/src/` - Synced Luau files written by the server

## How It Works

- Studio changes are sent to the Python server
- The server writes those changes to `MyGame/src`
- Local file changes are detected by the server
- The Studio plugin polls for those file changes and updates scripts inside Studio

This currently supports:

- `Script`
- `LocalScript`
- `ModuleScript`

## Notes

- IDE changes must be saved to disk before Studio can see them
- Enabling Auto Save in VS Code is recommended
- This project syncs script source, not every Roblox instance type
- I'll soon update the project so its compatibile with Github and other Git host's

## Troubleshooting

### Studio changes do not appear in the folder

- Make sure `export_server.py` is running
- Make sure Studio HTTP requests are enabled
- Make sure the plugin is loaded correctly

### IDE changes do not appear in Studio

- Make sure the file was actually saved
- Try enabling Auto Save in your editor
- Make sure the changed file is inside `MyGame/src`

### Open Studio script tabs do not refresh correctly

The plugin uses `ScriptEditorService` for better live updates, but if Studio behaves oddly, reopen the tab once and test again.

## Support

if you have any troubles or questions open an Issue on this repository and ill 100% respond. 
