import os
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, TestCase

import yt_dlp

from .forms import DownloadForm
from .views import _format_options, _ydl_options, download_file, download_video


class DownloadFormTests(TestCase):
    def test_accepts_supported_platform_links(self):
        urls = [
            'https://www.youtube.com/watch?v=example',
            'https://youtu.be/example',
            'https://www.instagram.com/reel/example/',
            'https://www.instagram.com/p/example/',
            'https://www.instagram.com/stories/user/123/',
            'https://www.instagram.com/tv/example/',
            'https://twitter.com/user/status/123',
            'https://x.com/user/status/123',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(DownloadForm({'url': url}).is_valid())

    def test_rejects_unrelated_or_lookalike_hosts(self):
        for url in ('https://example.com/video', 'https://youtube.com.example.org/watch?v=x'):
            with self.subTest(url=url):
                form = DownloadForm({'url': url})
                self.assertFalse(form.is_valid())


class FormatOptionTests(TestCase):
    def test_groups_by_height_prefers_mp4_and_adds_audio_size(self):
        options = _format_options({
            'formats': [
                {'height': 1080, 'vcodec': 'avc1', 'acodec': 'none', 'ext': 'mp4', 'filesize': 100_000_000},
                {'height': 1080, 'vcodec': 'vp9', 'acodec': 'none', 'ext': 'webm', 'filesize': 150_000_000},
                {'height': 720, 'vcodec': 'avc1', 'acodec': 'mp4a', 'ext': 'mp4', 'filesize_approx': 50_000_000},
                {'vcodec': 'none', 'acodec': 'opus', 'ext': 'webm', 'filesize': 10_000_000},
                {'vcodec': 'none', 'acodec': 'opus', 'ext': 'webm', 'filesize': 20_000_000},
            ],
        })

        self.assertEqual([item['label'] for item in options], ['1080p', '720p', 'Audio only (MP3)'])
        self.assertEqual(options[0]['file_size'], '~120.0 MB')
        self.assertEqual(options[1]['file_size'], '~50.0 MB')

    def test_unknown_size_is_not_rendered_as_none(self):
        options = _format_options({
            'formats': [{'height': 360, 'vcodec': 'avc1', 'acodec': 'mp4a', 'ext': 'mp4'}],
        })
        self.assertEqual(options[0]['file_size'], '~unknown')

    def test_cookie_file_environment_setting_is_passed_to_yt_dlp(self):
        with patch.dict(os.environ, {'YTDL_COOKIES_FILE': r'C:\private\cookies.txt'}):
            self.assertEqual(_ydl_options()['cookiefile'], r'C:\private\cookies.txt')


class DownloadViewTests(TestCase):
    def test_info_page_shows_platform_and_options(self):
        with patch('ytdl.views.yt_dlp.YoutubeDL') as youtube_dl:
            youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
                'title': 'A test video',
                'formats': [{'height': 720, 'vcodec': 'avc1', 'acodec': 'mp4a', 'ext': 'mp4', 'filesize': 1024}],
            }
            request = RequestFactory().post('/', {'url': 'https://x.com/user/status/123'})
            response = download_video(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<strong>X</strong>', response.content)
        self.assertIn(b'720p', response.content)
        self.assertIn(b'Audio only (MP3)', response.content)

    def test_extraction_error_shows_friendly_login_message(self):
        with patch('ytdl.views.yt_dlp.YoutubeDL') as youtube_dl:
            youtube_dl.return_value.__enter__.return_value.extract_info.side_effect = (
                yt_dlp.utils.DownloadError('Private video; sign in')
            )
            request = RequestFactory().post('/', {'url': 'https://www.instagram.com/reel/example/'})
            response = download_video(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn(b'This content needs a logged-in session or is private.', response.content)

    def test_download_returns_attachment_and_removes_temp_directory_on_close(self):
        created = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.assert_download_options()
                path = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4'))
                path.write_bytes(b'video with audio')
                created['directory'] = path.parent
                return {'title': 'A: test/video?'}

            def assert_download_options(self):
                assert self.options['format'] == 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
                assert self.options['merge_output_format'] == 'mp4'

        with patch('ytdl.views.yt_dlp.YoutubeDL', FakeYoutubeDL):
            response = self.client.post('/download/', {
                'url': 'https://www.youtube.com/watch?v=example',
                'option': '1080',
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="A_ test_video_.mp4"')
        self.assertEqual(b''.join(response.streaming_content), b'video with audio')
        response.close()
        self.assertFalse(created['directory'].exists())

    def test_mp3_download_uses_ffmpeg_postprocessor(self):
        created = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                self.assert_mp3_options()
                path = Path(self.options['outtmpl'].replace('%(ext)s', 'mp3'))
                path.write_bytes(b'mp3 audio')
                created['directory'] = path.parent
                return {'title': 'Audio'}

            def assert_mp3_options(self):
                assert self.options['format'] == 'bestaudio/best'
                assert self.options['postprocessors'] == [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]

        with patch('ytdl.views.yt_dlp.YoutubeDL', FakeYoutubeDL):
            response = self.client.post('/download/', {
                'url': 'https://www.youtube.com/watch?v=example',
                'option': 'mp3',
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="Audio.mp3"')
        self.assertEqual(b''.join(response.streaming_content), b'mp3 audio')
        response.close()
        self.assertFalse(created['directory'].exists())
