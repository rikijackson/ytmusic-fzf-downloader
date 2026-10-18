"""Filesystem and workflow regression checks; no network or media downloads."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import browse
import download
import handoff


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.music = patch.object(download.config, 'music_dir', self.root)
        self.music.start()
        self.addCleanup(self.music.stop)
        self.commands = []
        self.runner = patch.object(download.subprocess, 'run', side_effect=self.fake_download)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.credits = patch.object(download.yt, 'get_song_credits', return_value={})
        self.get_credits = self.credits.start()
        self.addCleanup(self.credits.stop)

    def fake_download(self, command, cwd, check):
        self.commands.append(command)
        template = command[command.index('-o') + 1]
        filename = template.replace('%(ext)s', 'opus').replace('%%', '%')
        source = Path(cwd) / filename
        source.write_bytes(b'audio')
        # These must never be chosen as symlink targets.
        source.with_suffix('.jpg').write_bytes(b'art')
        source.with_suffix('.webm').write_bytes(b'intermediate')
        record = command[command.index('--print-to-file') + 2]
        Path(record).write_text(str(source) + '\n')
        return subprocess.CompletedProcess(command, 0)

    def track(self, **kwargs):
        return dict(title='A/B 100%', videoId='abcdefghijk', trackNumber=5,
                    artists=[{'name': 'Primary'}, {'name': 'Guest'}], **kwargs)

    def test_album_numbering_final_audio_and_opt_in(self):
        directory = self.root / 'Primary' / 'Album'
        track = self.track(creditsBrowseId='MPTCtest')
        download.download_selected_songs_in_album([track], directory, False)
        source = directory / '05.A_B 100% [abcdefghijk].opus'
        self.assertTrue(source.is_file())
        self.assertFalse((self.root / 'Guest').exists())
        self.get_credits.assert_not_called()
        download.download_selected_songs_in_album([track], directory, False, True)
        link = self.root / 'Guest' / 'Features' / 'Album' / source.name
        self.assertEqual(link.resolve(), source)
        self.assertFalse(Path(os.readlink(link)).is_absolute())
        self.assertEqual(len(self.commands), 1)
        original_inode = link.lstat().st_ino
        download.download_selected_songs_in_album([track], directory, False, True)
        self.assertEqual(link.lstat().st_ino, original_inode)

    def test_preferred_performers_do_not_change_canonical_artist(self):
        self.get_credits.return_value = {'performed_by': {'data': [
            'Guest', 'Primary', 'New/Guest', 'New:Guest', 'Guest']}}
        directory = self.root / 'Primary' / 'Album'
        track = self.track(creditsBrowseId='MPTCtest')
        download.download_selected_songs_in_album([track], directory, False, True)
        source = next(directory.glob('*.opus'))
        links = list(self.root.rglob('Features/Album/*.opus'))
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link.resolve() == source for link in links))
        self.assertFalse((self.root / 'Primary' / 'Features').exists())

    def test_credits_empty_missing_and_failure_fall_back(self):
        for result in [{}, {'performed_by': {'data': []}}, RuntimeError('offline')]:
            with self.subTest(result=result):
                self.get_credits.side_effect = result if isinstance(result, Exception) else None
                self.get_credits.return_value = result
                self.assertEqual(download.performers(self.track(creditsBrowseId='MPTCtest')),
                                 ['Primary', 'Guest'])
        self.get_credits.reset_mock()
        self.assertEqual(download.performers(self.track()), ['Primary', 'Guest'])
        self.get_credits.assert_not_called()

    def test_stale_links_replaced_regular_files_preserved(self):
        directory = self.root / 'Primary' / 'Album'
        download.download_selected_songs_in_album([self.track()], directory, False, True)
        source = next(directory.glob('*.opus'))
        link = self.root / 'Guest' / 'Features' / 'Album' / source.name
        for target in ['missing.opus', self.root / 'other.opus']:
            if isinstance(target, Path):
                target.write_bytes(b'other')
            link.unlink()
            link.symlink_to(target)
            download.download_selected_songs_in_album([self.track()], directory, False, True)
            self.assertEqual(link.resolve(), source)
        link.unlink()
        link.write_bytes(b'keep me')
        download.download_selected_songs_in_album([self.track()], directory, False, True)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), b'keep me')

    def test_song_search_retains_all_artists_and_single_links(self):
        artists = [{'name': 'Primary'}, {'name': 'Guest'}]
        with patch.object(browse.yt, 'search', return_value=[{
            'videoId': 'abcdefghijk', 'title': 'Song', 'artists': artists}]), patch.object(
                browse.yt, 'get_song', return_value={'videoDetails': {
                    'videoId': 'abcdefghijk', 'title': 'Song', 'author': 'Channel'}}):
            items = browse.get_items('song', 'query')
        with patch.object(handoff.picker, 'multi_select_songs', return_value=items):
            handoff.decide_and_download('song', items, False, True)
        source = self.root / 'Primary' / 'Singles' / 'Song [abcdefghijk].opus'
        link = self.root / 'Guest' / 'Features' / 'Singles' / source.name
        self.assertEqual(link.resolve(), source)

    def test_artist_singles_share_directory_without_losing_selections(self):
        singles = [{'id': str(n), 'title': 'Single', 'type': 'single'} for n in range(2)]
        def album(album_id):
            track = self.track()
            track['videoId'] = f'abcdefghij{album_id}'
            return {'title': 'Single', 'artists': [{'name': 'Primary'}], 'tracks': [track]}
        with patch.object(browse.yt, 'get_album', side_effect=album), patch.object(
                handoff.picker, 'multi_select_different_media', return_value=singles), patch.object(
                handoff.picker, 'multi_select_songs', side_effect=lambda tracks, header: tracks):
            handoff.decide_and_download('artist', singles, False, True)
        self.assertEqual(len(list((self.root / 'Primary' / 'Singles').glob('*.opus'))), 2)
        self.assertEqual(len(list((self.root / 'Guest' / 'Features' / 'Singles').glob('*.opus'))), 2)

    def test_album_context_overrides_first_track_performer(self):
        album = {'title': 'A/B', 'artists': [{'name': 'Album/Artist'}],
                 'tracks': [self.track()]}
        with patch.object(browse.yt, 'get_album', return_value=album), patch.object(
                handoff.picker, 'multi_select_songs', side_effect=lambda tracks, header: tracks):
            tracks, directory = handoff.get_album_tracks_and_dir(
                {'id': 'album', 'title': 'A/B', 'type': 'album'})
        download.download_selected_songs_in_album(tracks, directory, False, True)
        self.assertEqual(directory, self.root / 'Album_Artist' / 'A_B')
        self.assertTrue((self.root / 'Primary' / 'Features' / 'A_B').is_dir())

    def test_duplicate_detection_ignores_sidecars_directories_and_broken_links(self):
        (self.root / 'art [abcdefghijk].jpg').touch()
        (self.root / 'partial [abcdefghijk].opus.part').touch()
        (self.root / 'directory [abcdefghijk].opus').mkdir()
        (self.root / 'link [abcdefghijk].opus').symlink_to('missing')
        (self.root / 'substring [xabcdefghijk].opus').touch()
        self.assertFalse(download.is_already_downloaded('abcdefghijk'))
        old = self.root / 'old [abcdefghijk].mp3'
        old.touch()
        self.assertTrue(download.is_already_downloaded('abcdefghijk'))
        download.download_song({'id': 'abcdefghijk', 'title': 'Song', 'artists': 'Primary'}, False)
        self.assertEqual(self.commands, [])
        download.download_song({'id': 'abcdefghijk', 'title': 'Song', 'artists': 'Primary'}, True)
        self.assertEqual(len(self.commands), 1)

    def test_failed_download_does_not_create_links(self):
        self.runner.stop()
        with patch.object(download.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'yt-dlp')):
            with self.assertRaises(subprocess.CalledProcessError):
                download.download_song({'id': 'abcdefghijk', 'title': 'Song',
                                        'artists': ['Primary', 'Guest']}, False, True)
        self.assertFalse((self.root / 'Guest').exists())

    def test_sanitize_cannot_escape_component(self):
        for name in ['.', '..', '', '  ', '/', '../', 'a/b', 'a\\b', 'a\x00b', 'a\nb']:
            component = download.sanitize(name)
            self.assertNotIn(component, ('', '.', '..'))
            self.assertEqual(Path(component).name, component)
            self.assertNotIn('\\', component)
            self.assertFalse(any(ord(char) < 32 for char in component))


if __name__ == '__main__':
    unittest.main()
