import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import suite  # noqa: E402


def test_every_eval_check_tells_its_known_good_play_from_its_bad_one():
    # claimed-deed's check held two backspaces where \b was meant and never matched; its passes meant nothing
    assert suite.check_the_checks() == []
