"""The complete runtime bundle. User data and old staged installers are excluded."""

FULL_FILES = (
    'album_info.py', 'app_icon.py', 'control_client.py', 'copy_update_file.py',
    'destination_settings.py', 'download_album.py', 'ongoing.py', 'probe_vk_api.py',
    'publication_state.py', 'publish_album_v3.py', 'publish_control.py',
    'publisher_controller.py', 'publisher_runtime.py', 'snapshot_worker.py',
    'status_app.py', 'vk_api.py', 'vk_session.py', 'vk_source.py', 'worker_status.py',
    'control_task.ps1', 'prepare_env.ps1', 'status_task.ps1', 'task.ps1', 'task_bridge.ps1',
    '_run.cmd', 'CHECK_AUTO.cmd', 'CHECK_CONNECTION.cmd', 'IMPORT_VK_SESSION.cmd',
    'OPEN_STATUS.cmd', 'PROBE_VK_API.cmd', 'PUBLISH_ALBUM.cmd', 'PUBLISH_LOCAL.cmd',
    'SETUP.cmd', 'START_AUTO.cmd', 'STOP_AUTO.cmd',
    'requirements.lock', 'requirements-ui.lock', 'README.txt', 'README_AUTO.txt', 'README_V3.txt',
)

PRESERVED_FILES = ('config.json', 'token.dpapi', 'vk_api_token.dpapi')
