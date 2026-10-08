"""Create a reproducible submission archive without secrets or runtime state."""
from pathlib import Path
import zipfile
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.venv', 'venv', '.git', '.code_review_agent', '.pytest_cache', '__pycache__', 'node_modules', 'test-results', 'playwright-report', 'submission'}
DESTINATION = ROOT / '2412190525曾焕潇.zip'


def package():
    if not (ROOT / 'frontend' / 'dist' / 'index.html').exists():
        raise SystemExit('请先在 frontend 执行 npm ci 和 npm run build。')
    if DESTINATION.exists():
        backup = ROOT / ".code_review_agent" / "archive-backups" / "original-submission.zip"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DESTINATION, backup)
    with zipfile.ZipFile(DESTINATION, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob('*')):
            relative = path.relative_to(ROOT)
            if not path.is_file() or any(part in EXCLUDED or part.endswith('.egg-info') for part in relative.parts):
                continue
            if path.suffix in {'.zip', '.pyc', '.pyo', '.tsbuildinfo'} or path.name == '.DS_Store':
                continue
            if path.name.startswith('.env') and path.name != '.env.example':
                continue
            archive.write(path, relative.as_posix())
    print(f'{DESTINATION.name}: {DESTINATION.stat().st_size / 1024 / 1024:.2f} MB')


if __name__ == '__main__':
    package()
