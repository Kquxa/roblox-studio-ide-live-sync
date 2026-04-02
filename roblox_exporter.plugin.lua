-- Roblox live-sync plugin.
-- Keeps Studio scripts and local files in sync through the Python bridge server.

local HttpService = game:GetService("HttpService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local ScriptEditorService = game:GetService("ScriptEditorService")

local BASE_URL = "http://127.0.0.1:34873"
local SYNC_URL = BASE_URL .. "/sync"
local MANIFEST_URL = BASE_URL .. "/manifest"
local FILE_URL = BASE_URL .. "/file"
local EVENTS_URL = BASE_URL .. "/events"
local FIXED_PROJECT_ROOT = "MyGame"
local POLL_INTERVAL = 1

local SCRIPT_TYPES = {
	Script = true,
	LocalScript = true,
	ModuleScript = true,
}

local SCRIPT_CLASS_BY_TYPE = {
	Script = "Script",
	LocalScript = "LocalScript",
	ModuleScript = "ModuleScript",
}

local toolbar = plugin:CreateToolbar("Roblox Exporter")
local export_button = toolbar:CreateButton(
	"Export Scripts",
	"Force-sync all Script/LocalScript/ModuleScript instances to disk",
	""
)
local import_button = toolbar:CreateButton(
	"Import Scripts",
	"Force-sync all script files from disk into Studio",
	""
)

local studio_snapshot = {}
local disk_event_cursor = 0
local import_guard = {}
local import_guard_ttl = 5

local function now_seconds(): number
	return os.clock()
end

local function script_key(service_name: string, path_parts: {string}, name: string, script_type: string): string
	return table.concat({
		service_name,
		table.concat(path_parts, "/"),
		name,
		script_type,
	}, "::")
end

local function get_top_level_service(instance: Instance): Instance?
	local current = instance
	while current and current.Parent and current.Parent ~= game do
		current = current.Parent
	end

	if current and current.Parent == game then
		return current
	end

	return nil
end

local function get_relative_path(service: Instance, script_instance: Instance): {string}?
	local parts = {}
	local cursor = script_instance.Parent

	while cursor and cursor ~= service do
		table.insert(parts, 1, cursor.Name)
		cursor = cursor.Parent
	end

	if cursor ~= service then
		return nil
	end

	return parts
end

local function read_source(script_instance: Instance): string?
	local ok, source_or_error = pcall(function()
		return (script_instance :: any).Source
	end)

	if ok then
		return source_or_error
	end

	warn(
		("[RobloxExporter] Could not read Source for %s: %s"):format(
			script_instance:GetFullName(),
			tostring(source_or_error)
		)
	)
	return nil
end

local function collect_scripts_indexed()
	local indexed = {}

	for _, instance in ipairs(game:GetDescendants()) do
		if SCRIPT_TYPES[instance.ClassName] then
			local service = get_top_level_service(instance)
			if service then
				local relative_path = get_relative_path(service, instance)
				if relative_path then
					local source = read_source(instance)
					if source ~= nil then
						local key = script_key(service.Name, relative_path, instance.Name, instance.ClassName)
						indexed[key] = {
							service = service.Name,
							path = relative_path,
							name = instance.Name,
							type = instance.ClassName,
							source = source,
						}
					end
				end
			end
		end
	end

	return indexed
end

local function post_sync(upserts, deletes)
	local payload = {
		projectRoot = FIXED_PROJECT_ROOT,
		upserts = upserts,
		deletes = deletes,
	}

	local body = HttpService:JSONEncode(payload)
	local ok, response_or_error = pcall(function()
		return HttpService:PostAsync(
			SYNC_URL,
			body,
			Enum.HttpContentType.ApplicationJson
		)
	end)

	if not ok then
		warn("[RobloxExporter] Sync failed: " .. tostring(response_or_error))
		warn("[RobloxExporter] Make sure export_server.py is running on 127.0.0.1:34873.")
		return false
	end

	return true, response_or_error
end

local function http_get_json(url: string)
	local ok, result = pcall(function()
		return HttpService:GetAsync(url)
	end)

	if not ok then
		return nil, "HTTP request failed: " .. tostring(result)
	end

	local ok_decode, data_or_error = pcall(function()
		return HttpService:JSONDecode(result)
	end)

	if not ok_decode then
		return nil, "Failed to decode JSON: " .. tostring(data_or_error)
	end

	return data_or_error, nil
end

local function http_get_file(rel_file: string)
	local url = FILE_URL
		.. "?projectRoot=" .. HttpService:UrlEncode(FIXED_PROJECT_ROOT)
		.. "&relFile=" .. HttpService:UrlEncode(rel_file)
	local data, err = http_get_json(url)
	if not data then
		return nil, err
	end
	return data.source, nil
end

local function mark_import_guard(entry, source)
	local key = script_key(entry.service, entry.path or {}, entry.name, entry.type)
	import_guard[key] = {
		untilTime = now_seconds() + import_guard_ttl,
		source = source,
	}
end

local function cleanup_import_guard()
	local current_time = now_seconds()
	for key, info in pairs(import_guard) do
		if info.untilTime <= current_time then
			import_guard[key] = nil
		end
	end
end

local function ensure_container(current: Instance, name: string): Instance
	local child = current:FindFirstChild(name)
	if child then
		return child
	end

	local folder = Instance.new("Folder")
	folder.Name = name
	folder.Parent = current
	return folder
end

local function ensure_target_instance(entry)
	local service = game:FindFirstChild(entry.service)
	if not service then
		return nil, "missing service"
	end

	local current = service
	for _, part in ipairs(entry.path or {}) do
		current = ensure_container(current, part)
	end

	local target = current:FindFirstChild(entry.name)
	if not target then
		local class_name = SCRIPT_CLASS_BY_TYPE[entry.type]
		if not class_name then
			return nil, "invalid script type"
		end

		target = Instance.new(class_name)
		target.Name = entry.name
		target.Parent = current
	end

	if target.ClassName ~= entry.type then
		return nil, "type mismatch"
	end

	return target, nil
end

local function find_target_instance(entry)
	local service = game:FindFirstChild(entry.service)
	if not service then
		return nil
	end

	local current = service
	for _, part in ipairs(entry.path or {}) do
		current = current:FindFirstChild(part)
		if not current then
			return nil
		end
	end

	local target = current:FindFirstChild(entry.name)
	if not target or target.ClassName ~= entry.type then
		return nil
	end

	return target
end

local function apply_disk_entry(entry)
	if entry.op == "delete" then
		local target = find_target_instance(entry)
		if target then
			target:Destroy()
		end
		return true
	end

	local source, file_err = http_get_file(entry.relFile)
	if not source then
		return false, file_err
	end

	local target, reason = ensure_target_instance(entry)
	if not target then
		return false, reason
	end

	mark_import_guard(entry, source)

	local ok, set_err = pcall(function()
		ScriptEditorService:UpdateSourceAsync(target, function(current_source)
			if current_source == source then
				return nil
			end
			return source
		end)
	end)

	if not ok then
		return false, tostring(set_err)
	end

	return true
end

local function refresh_snapshot()
	studio_snapshot = collect_scripts_indexed()
end

local function sync_studio_changes(full_push: boolean?)
	cleanup_import_guard()

	local current = collect_scripts_indexed()
	local upserts = {}
	local deletes = {}

	for key, entry in pairs(current) do
		local guard = import_guard[key]
		local previous = studio_snapshot[key]
		local changed = full_push or previous == nil or previous.source ~= entry.source

		if guard and guard.source == entry.source then
			import_guard[key] = nil
			changed = false
		end

		if changed then
			table.insert(upserts, {
				service = entry.service,
				path = entry.path,
				name = entry.name,
				type = entry.type,
				source = entry.source,
			})
		end
	end

	for key, previous in pairs(studio_snapshot) do
		if current[key] == nil then
			table.insert(deletes, {
				service = previous.service,
				path = previous.path,
				name = previous.name,
				type = previous.type,
			})
		end
	end

	if #upserts > 0 or #deletes > 0 then
		local ok = post_sync(upserts, deletes)
		if ok then
			print(("[RobloxExporter] Studio live sync: upserts=%d deletes=%d"):format(#upserts, #deletes))
		end
	end

	studio_snapshot = current
end

local function run_full_import()
	local manifest_url = MANIFEST_URL .. "?projectRoot=" .. HttpService:UrlEncode(FIXED_PROJECT_ROOT)
	local manifest, err = http_get_json(manifest_url)
	if not manifest then
		warn("[RobloxExporter] Import failed: " .. err)
		warn("[RobloxExporter] Make sure export_server.py is running on 127.0.0.1:34873.")
		return
	end

	if type(manifest) ~= "table" then
		warn("[RobloxExporter] Import failed: malformed manifest response.")
		return
	end

	ChangeHistoryService:SetWaypoint("Roblox Exporter Import Start")

	local imported = 0
	local skipped = 0

	for _, entry in ipairs(manifest) do
		local ok, apply_err = apply_disk_entry({
			op = "upsert",
			service = entry.service,
			path = entry.path,
			name = entry.name,
			type = entry.type,
			relFile = entry.relFile,
		})
		if ok then
			imported += 1
		else
			skipped += 1
			warn(("[RobloxExporter] Import skipped %s: %s"):format(
				tostring(entry.relFile or entry.name or "?"),
				tostring(apply_err)
			))
		end
	end

	ChangeHistoryService:SetWaypoint("Roblox Exporter Import Complete")
	refresh_snapshot()

	print(("[RobloxExporter] Import complete. Imported=%d Skipped=%d"):format(imported, skipped))
end

local function poll_disk_events()
	local url = EVENTS_URL
		.. "?projectRoot=" .. HttpService:UrlEncode(FIXED_PROJECT_ROOT)
		.. "&since=" .. HttpService:UrlEncode(tostring(disk_event_cursor))

	local payload, err = http_get_json(url)
	if not payload then
		warn("[RobloxExporter] Live import poll failed: " .. err)
		return
	end

	local events = payload.events
	if type(events) ~= "table" then
		return
	end

	local applied_any = false

	for _, entry in ipairs(events) do
		if entry.source == "disk" then
			local ok, apply_err = apply_disk_entry(entry)
			if not ok then
				warn(("[RobloxExporter] Live import skipped %s: %s"):format(
					tostring(entry.relFile or entry.name or "?"),
					tostring(apply_err)
				))
			else
				applied_any = true
			end
		end

		if type(entry.sequence) == "number" then
			disk_event_cursor = math.max(disk_event_cursor, entry.sequence)
		end
	end

	if applied_any then
		ChangeHistoryService:SetWaypoint("Roblox Exporter Live Import")
		refresh_snapshot()
	end
end

local function start_live_sync_loop()
	task.spawn(function()
		refresh_snapshot()
		sync_studio_changes(true)
		run_full_import()

		while true do
			sync_studio_changes(false)
			poll_disk_events()
			task.wait(POLL_INTERVAL)
		end
	end)
end

export_button.Click:Connect(function()
	sync_studio_changes(true)
end)

import_button.Click:Connect(function()
	run_full_import()
end)

start_live_sync_loop()
