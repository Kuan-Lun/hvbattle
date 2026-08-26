"""Suite-wide safeguards for process-global third-party behaviour."""

import os

# Set this before test-module collection imports ponychart-classifier. ONNX
# Runtime 1.29 otherwise creates a `:memory:.ses` telemetry artifact in the
# repository root even when collection does not run a test.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
