import zipfile, os

src = r'D:\Projects\agentic-sre\investigation-agent\package'
out = r'D:\Projects\agentic-sre\investigation-agent\lambda-package.zip'

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if f.endswith('.pyc'):
                continue
            fp = os.path.join(root, f)
            zf.write(fp, os.path.relpath(fp, src))

size_mb = round(os.path.getsize(out) / 1024 / 1024, 1)
print(f'Done: {out} ({size_mb} MB)')
