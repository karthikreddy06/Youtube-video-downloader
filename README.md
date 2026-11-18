# Multi-platform Video Downloader

A local Django app for downloading videos from YouTube, Instagram, and X. It lists available resolutions, merges video with audio, and can extract MP3 audio. Some content may require a logged-in session.

## Requirements

- Python 3.10 or newer
- FFmpeg available on `PATH`

## Setup (PowerShell)

```powershell
Set-Location .\YouTube-Downloader
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>.

## Optional cookies

For content that requires your own logged-in session, export your cookies in Netscape format and set `YTDL_COOKIES_FILE` to the file path before starting the server:

```powershell
$env:YTDL_COOKIES_FILE = 'C:\path\to\cookies.txt'
```

Do not share or commit your cookies file.

Only download content you own or have permission to save.
