"""
Upload the app's data to its private Cloudflare R2 bucket, only the files that changed:

    python etl/r2_sync.py model pl/model
    python etl/r2_sync.py data pl/data --exclude snapshots

R2 speaks the S3 API.  Credentials come from the environment (repository secrets in Actions):
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.  Without them the script lists what it would upload
and exits 0, so a fork or a run without secrets does nothing.

A file is skipped when the bucket already holds the same bytes: every file is sent in one PUT (no multipart), so
its ETag is its MD5.  Uploads run WORKERS at a time (one at a time took about a second per file from a runner).
Files removed locally are not deleted from the bucket unless --delete is given.
"""
import argparse
import hashlib
import mimetypes
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

WORKERS = 16

TYPES = {".json": "application/json", ".geojson": "application/geo+json", ".csv": "text/csv; charset=utf-8", ".pbf": "application/x-protobuf"}


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_files(root, exclude):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if (d if rel_dir == "." else f"{rel_dir}/{d}") not in exclude]
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel not in exclude:
                yield rel, os.path.join(dirpath, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("prefix")
    ap.add_argument("--exclude", nargs="*", default=[], help="paths relative to src (folders or files)")
    ap.add_argument("--delete", action="store_true", help="remove objects under prefix that no longer exist locally")
    args = ap.parse_args()
    prefix = args.prefix.strip("/") + "/"
    files = dict(local_files(args.src, set(args.exclude)))

    env = {k: os.environ.get(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
    if not all(env.values()):
        size = sum(os.path.getsize(p) for p in files.values())
        print(f"R2 sync: no credentials; would upload up to {len(files)} files ({size / 1e6:.0f} MB) from {args.src} to {prefix}")
        return

    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name="auto",
                      config=Config(retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=WORKERS * 2))
    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=env["R2_BUCKET"], Prefix=prefix):
        for o in page.get("Contents", []):
            remote[o["Key"][len(prefix):]] = o["ETag"].strip('"')
    todo = [(rel, path) for rel, path in sorted(files.items()) if remote.get(rel) != md5(path)]
    skipped = len(files) - len(todo)
    total_mb = sum(os.path.getsize(p) for _, p in todo) / 1e6
    print(f"R2 sync {args.src} -> {prefix}: {len(todo)} files to upload ({total_mb:.0f} MB), {skipped} unchanged", flush=True)

    def put(item):
        rel, path = item
        ext = os.path.splitext(rel)[1].lower()
        ctype = TYPES.get(ext) or mimetypes.guess_type(rel)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            # one PUT, so the stored ETag is the file's MD5 and the next run can skip it
            s3.put_object(Bucket=env["R2_BUCKET"], Key=prefix + rel, Body=f.read(), ContentType=ctype)
        return rel

    sent, failed, t0 = 0, [], time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(put, item): item[0] for item in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
                sent += 1
            except Exception as e:  # keep going; a rerun picks up what failed
                failed.append(futures[fut])
                print(f"R2 sync: {futures[fut]} failed: {e}", flush=True)
            if (sent + len(failed)) % 250 == 0:
                print(f"R2 sync: {sent + len(failed)} of {len(todo)} done in {time.time() - t0:.0f} s", flush=True)
    gone = sorted(set(remote) - set(files))
    if args.delete and gone:
        for i in range(0, len(gone), 1000):
            s3.delete_objects(Bucket=env["R2_BUCKET"], Delete={"Objects": [{"Key": prefix + k} for k in gone[i:i + 1000]]})
    print(f"R2 sync {args.src} -> {prefix}: {sent} uploaded in {time.time() - t0:.0f} s, {len(failed)} failed, {skipped} unchanged, "
          f"{len(gone)} only in the bucket{' (deleted)' if args.delete else ''}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
