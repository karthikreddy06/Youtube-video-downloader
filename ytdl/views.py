import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from django.http import FileResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_http_methods

from .forms import DownloadForm


logger = logging.getLogger(__name__)
DOWNLOAD_LOCK = threading.Lock()
YTDL_ERRORS = (
    yt_dlp.utils.DownloadError,
    yt_dlp.utils.UnsupportedError,
    yt_dlp.utils.PostProcessingError,
)


def _ydl_options():
    options = {'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True}
    cookies_file = os.environ.get('YTDL_COOKIES_FILE')
    if cookies_file:
        options['cookiefile'] = cookies_file
    return options


def _platform_for_url(url):
    host = urlsplit(url).hostname or ''
    if host == 'youtu.be' or host == 'youtube.com' or host.endswith('.youtube.com'):
        return 'YouTube'
    if host == 'instagram.com' or host.endswith('.instagram.com'):
        return 'Instagram'
    return 'X'


def _file_size(formats):
    sizes = [
        item.get('filesize') or item.get('filesize_approx')
        for item in formats
    ]
    return max((size for size in sizes if size), default=None)


def _format_options(info):
    formats = info.get('formats') or []
    audio_formats = [
        item for item in formats
        if item.get('vcodec') == 'none'
    ]
    audio_size = _file_size(audio_formats)
    by_height = {}

    for item in formats:
        height = item.get('height')
        if item.get('vcodec') == 'none' or not height:
            continue

        size = item.get('filesize') or item.get('filesize_approx')
        if item.get('acodec') == 'none':
            size = size + audio_size if size and audio_size else None
        candidate = {
            'height': height,
            'label': '{}p'.format(height),
            'file_size': '~unknown' if size is None else '~{:.1f} MB'.format(size / 1_000_000),
            'extension': item.get('ext', ''),
            'size': size,
        }
        current = by_height.get(height)
        candidate_rank = (candidate['extension'] == 'mp4', size or -1)
        current_rank = (
            (current['extension'] == 'mp4', current['size'] or -1)
            if current else None
        )
        if current is None or candidate_rank > current_rank:
            by_height[height] = candidate

    options = sorted(by_height.values(), key=lambda item: item['height'], reverse=True)
    for item in options:
        del item['size']
        del item['extension']
    options.append({'label': 'Audio only (MP3)', 'value': 'mp3', 'file_size': ''})
    for item in options[:-1]:
        item['value'] = str(item['height'])
        del item['height']
    return options


def _friendly_download_error(error):
    message = str(error).lower()
    if any(term in message for term in ('429', 'rate-limit', 'rate limit', 'too many requests')):
        return 'The service is receiving too many requests. Please wait and try again.'
    if any(term in message for term in (
        'login', 'private', 'sign in', 'authentication', 'please log in',
    )):
        return 'This content needs a logged-in session or is private.'
    if any(term in message for term in (
        'requested format is not available',
        'requested format not available',
        'no video formats found',
        'format is not available',
        'format not available',
    )):
        return 'The requested format is not available. Please choose another quality.'
    if 'ffmpeg' in message and any(term in message for term in (
        'not found', 'not installed', 'no such file', 'required', 'executable',
    )):
        return 'FFmpeg is missing or unavailable. Install FFmpeg and ensure it is on PATH.'
    return 'Could not process this link. Check that it is public and try again.'


def _is_format_unavailable(error):
    message = str(error).lower()
    return any(term in message for term in (
        'requested format is not available',
        'requested format not available',
        'no video formats found',
        'format is not available',
        'format not available',
    ))


def _download_video_with_fallback(url, options, height):
    selectors = [
        'bestvideo[height<={}]+bestaudio/best[height<={}]'.format(height, height),
        'bestvideo+bestaudio/best',
        'best',
    ]
    for index, selector in enumerate(selectors):
        try:
            with yt_dlp.YoutubeDL(dict(options, format=selector)) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as error:
            if index == len(selectors) - 1 or not _is_format_unavailable(error):
                raise
            logger.warning(
                'yt-dlp format selector %r failed; trying the next selector: %s',
                selector,
                error,
                exc_info=True,
            )


def _safe_filename(title):
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')
    if not filename:
        filename = 'download'
    if filename.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.match(r'^(COM|LPT)[1-9]$', filename.upper()):
        filename = '_' + filename
    return filename[:180]


@require_http_methods(['GET', 'POST'])
def download_video(request):
    form = DownloadForm(request.POST or None)
    context = {'form': form}
    if request.method == 'GET':
        return render(request, 'home.html', context)
    if not form.is_valid():
        return render(request, 'home.html', context, status=400)

    video_url = form.cleaned_data['url']
    try:
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except YTDL_ERRORS as error:
        logger.exception('yt-dlp metadata extraction failed: %s', error)
        context['error'] = _friendly_download_error(error)
        return render(request, 'home.html', context, status=400)

    if not info:
        context['error'] = 'No video information was found for this link.'
        return render(request, 'home.html', context, status=400)

    thumbnail = info.get('thumbnail')
    if not thumbnail and info.get('thumbnails'):
        thumbnail = info['thumbnails'][-1].get('url')

    context.update({
        'source_url': video_url,
        'title': info.get('title') or 'Untitled video',
        'platform': _platform_for_url(video_url),
        'thumb': thumbnail,
        'duration': round(info['duration'] / 60, 2) if info.get('duration') is not None else None,
        'views': '{:,}'.format(info['view_count']) if info.get('view_count') is not None else None,
        'likes': info.get('like_count'),
        'options': _format_options(info),
    })
    return render(request, 'home.html', context)


@require_POST
def download_file(request):
    form = DownloadForm(request.POST)
    if not form.is_valid():
        return render(request, 'home.html', {'form': form}, status=400)

    option = request.POST.get('option', '')
    if option != 'mp3' and (not option.isdigit() or int(option) <= 0):
        return render(request, 'home.html', {
            'form': DownloadForm(initial={'url': form.cleaned_data['url']}),
            'error': 'Choose a valid download option.',
        }, status=400)

    if not DOWNLOAD_LOCK.acquire(blocking=False):
        return render(request, 'home.html', {
            'form': DownloadForm(initial={'url': form.cleaned_data['url']}),
            'error': 'Another download is in progress. Please try again shortly.',
        }, status=429)

    temp_dir = None
    response = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix='ytdl-'))
        options = _ydl_options()
        options['outtmpl'] = str(temp_dir / 'source.%(ext)s')
        options['quiet'] = True
        if option == 'mp3':
            options.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            height = int(option)
            options['merge_output_format'] = 'mp4'
            info = _download_video_with_fallback(form.cleaned_data['url'], options, height)
        if option == 'mp3':
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(form.cleaned_data['url'], download=True)

        files = [
            path for path in temp_dir.iterdir()
            if path.is_file() and path.suffix.lower() not in {'.part', '.ytdl', '.json', '.description'}
        ]
        if not files:
            raise OSError('yt-dlp completed without producing a media file')
        output_path = max(files, key=lambda path: path.stat().st_size)
        title = _safe_filename((info or {}).get('title') or 'download')
        final_path = output_path.with_name(title + output_path.suffix.lower())
        output_path.replace(final_path)

        response = FileResponse(
            final_path.open('rb'),
            as_attachment=True,
            filename=final_path.name,
            content_type=mimetypes.guess_type(final_path.name)[0] or 'application/octet-stream',
        )
        response._resource_closers.append(lambda: shutil.rmtree(temp_dir))
        return response
    except YTDL_ERRORS as error:
        logger.exception('yt-dlp download failed: %s', error)
        return render(request, 'home.html', {
            'form': DownloadForm(initial={'url': form.cleaned_data['url']}),
            'error': _friendly_download_error(error),
        }, status=400)
    except OSError:
        return render(request, 'home.html', {
            'form': DownloadForm(initial={'url': form.cleaned_data['url']}),
            'error': 'The file could not be prepared. Please try again.',
        }, status=500)
    finally:
        DOWNLOAD_LOCK.release()
        if response is None and temp_dir is not None:
            shutil.rmtree(temp_dir)
