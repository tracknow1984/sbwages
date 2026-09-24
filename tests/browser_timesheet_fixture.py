"""Isolated localhost fixture for test_timesheet_browser.cjs (never uses production data)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_app import AppTests

if __name__ == '__main__':
    fixture = AppTests()
    fixture.setUp()
    try:
        fixture.add()
        fixture.app.run(host='127.0.0.1', port=5087, debug=False)
    finally:
        fixture.tearDown()
