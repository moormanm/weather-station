import hashlib
import sys

checksum = hashlib.sha256()
with open(sys.argv[1], "rb") as source:
    for block in iter(lambda: source.read(1024 * 1024), b""):
        checksum.update(block)
if checksum.hexdigest() != sys.argv[2]:
    sys.exit("The base image checksum does not match. Remove the cached file and retry.")
