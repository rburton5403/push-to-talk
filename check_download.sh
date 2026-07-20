#!/usr/bin/env bash
#
# Diagnose the S3 model download. Run this when `push_to_talk.py` hangs or
# errors while "downloading ... from S3". It checks whether this machine can
# actually reach the bucket, separately from any Python issue.
#
# Usage: ./check_download.sh
#
set -u

# Keep these in sync with push_to_talk.py (S3_BASE_URL / _MODEL_FILES).
BASE="${PTT_MODEL_S3:-https://rburton5403-push-to-talk-model.s3.us-east-2.amazonaws.com}"
CONFIG_KEY="models--mlx-community--parakeet-tdt-0.6b-v2/blobs/8955c588b5549ef70811f2121c6c8bda33508992"
WEIGHTS_KEY="models--mlx-community--parakeet-tdt-0.6b-v2/blobs/b958c37a6baa6874a279108755c8f2818e27bf647d72d54800a234a421341dfe"

echo "== proxy env (a stale proxy makes downloads hang) =="
env | grep -i proxy || echo "(no proxy vars set — good)"
echo

echo "== small file: full download to /dev/null (config.json, 36 KB) =="
curl -v --max-time 30 -o /dev/null "$BASE/$CONFIG_KEY"
small=$?
echo

echo "== large file: headers only, no download (model.safetensors, 2.3 GB) =="
curl -sS -I --max-time 30 "$BASE/$WEIGHTS_KEY" | grep -iE "HTTP/|content-length"
large=${PIPESTATUS[0]}
echo

echo "== result =="
if [ "$small" -eq 0 ] && [ "$large" -eq 0 ]; then
  echo "OK: this machine can reach S3. If the app still hangs, the problem is"
  echo "    Python-specific (check the proxy vars above; unset them and retry)."
elif [ "$small" -eq 28 ] || [ "$large" -eq 28 ]; then
  echo "TIMEOUT (curl exit 28): the network can't reach S3 within 30s."
  echo "    Likely a firewall, VPN, captive portal, or DNS issue on this network."
else
  echo "FAILED (curl exit small=$small large=$large). See the verbose output above;"
  echo "    a 403 means the bucket policy isn't public, 404 means a missing object."
fi
