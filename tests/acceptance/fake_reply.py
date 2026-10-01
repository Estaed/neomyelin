"""A stand-in model for acceptance tests: prints the canned reply named by NEOMYELIN_FAKE_REPLY."""
import os
import sys
from pathlib import Path

sys.stdin.read()  # the engine sends the prompt on stdin; a real CLI reads it all
sys.stdout.write(Path(os.environ['NEOMYELIN_FAKE_REPLY']).read_text(encoding='utf-8'))
