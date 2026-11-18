from django import forms
from urllib.parse import urlsplit


class DownloadForm(forms.Form):
    url = forms.CharField(
        widget=forms.URLInput(attrs={
            'placeholder': 'Paste a YouTube, Instagram or X link',
            'aria-label': 'Video link',
        }),
        label=False,
        max_length=2048,
    )

    def clean_url(self):
        url = self.cleaned_data['url'].strip()
        if '://' not in url:
            url = 'https://' + url

        parsed = urlsplit(url)
        hostname = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in {'http', 'https'} or not hostname or parsed.username or parsed.password:
            raise forms.ValidationError('Enter a valid link from YouTube, Instagram, or X.')

        def is_domain(domain):
            return hostname == domain or hostname.endswith('.' + domain)

        path_parts = [part for part in parsed.path.strip('/').split('/') if part]
        supported = (
            (is_domain('youtube.com') and bool(path_parts))
            or (hostname == 'youtu.be' and bool(path_parts))
            or (
                is_domain('instagram.com')
                and any(part in {'reel', 'reels', 'p', 'stories', 'tv'} for part in path_parts)
            )
            or (
                (is_domain('twitter.com') or is_domain('x.com'))
                and 'status' in path_parts
            )
        )
        if not supported:
            raise forms.ValidationError('Enter a supported YouTube, Instagram, or X video link.')
        return url
