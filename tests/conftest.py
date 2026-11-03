import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# keep test runs out of the live desk's logs/: agent + api loggers write to a temp dir during pytest
import tempfile
import agents.common as _common
_common.LOG_DIR = pathlib.Path(tempfile.mkdtemp(prefix="desk_test_logs_"))
