"""Самопроверка импорта архива: python tests/test_import.py (или pytest)."""
import io, os, sys, tempfile, zipfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ['DATA_DIR'] = str(tmp / 'data'); os.environ['PRIVATE_DIR'] = str(tmp / 'scripts')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import main


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items(): z.writestr(name, data)
    buf.seek(0); return buf


def test_import_creates_then_updates():
    main.import_zip(make_zip({'repo-main/README.md': '# Стенд A\n\n![x](docs/images/01.png)', 'repo-main/br-rtr.sh': 'echo v1',
                              'repo-main/docs/images/01.png': b'png', '__MACOSX/repo-main/._br-rtr.sh': 'junk'}), 'repo-main.zip')
    with main.db() as c:
        art = c.execute('SELECT * FROM articles').fetchall(); scr = c.execute('SELECT * FROM scripts').fetchall()
    assert len(art) == 1 and art[0]['title'] == 'Стенд A' and art[0]['full_md'] == '![x](/media/стенд-a/01.png)'
    assert (main.UPLOADS / 'стенд-a' / '01.png').read_bytes() == b'png'
    assert len(scr) == 1 and scr[0]['filename'][33:] == 'br-rtr.sh'  # мусор из __MACOSX не импортирован

    # тот же архив с новым содержимым: записи обновляются, ссылка (token) на скрипт не меняется
    main.import_zip(make_zip({'README.md': '# Стенд A\n\nv2', 'br-rtr.sh': 'echo v2'}), 'repo-main.zip')
    # другой архив с тем же именем картинки: отдельная статья, чужая картинка не затёрта
    main.import_zip(make_zip({'README.md': '# Стенд B', 'docs/images/01.png': b'other'}), 'b.zip')
    with main.db() as c:
        art = c.execute('SELECT * FROM articles ORDER BY id').fetchall(); scr2 = c.execute('SELECT * FROM scripts').fetchall()
    assert [a['title'] for a in art] == ['Стенд A', 'Стенд B'] and art[0]['full_md'].endswith('v2')
    assert len(scr2) == 1 and scr2[0]['token'] == scr[0]['token']
    assert (main.PRIVATE / scr2[0]['filename']).read_text() == 'echo v2'
    assert (main.UPLOADS / 'стенд-a' / '01.png').read_bytes() == b'png'


if __name__ == '__main__':
    test_import_creates_then_updates(); print('ok')
