"""Staff work-week profiles and immutable timesheet date ranges."""
from datetime import date, timedelta


WORK_WEEKS = {
    'mon_sun': {'label': 'Monday–Sunday', 'ending': 6, 'days': 7},
    'mon_fri': {'label': 'Monday–Friday', 'ending': 4, 'days': 5},
    'fri_next_sat': {'label': 'Friday to the following Saturday', 'ending': 5, 'days': 9},
}


def profile_end(day, profile):
    ending = WORK_WEEKS[profile]['ending']
    # Weekend visits to a Monday-Friday sheet show the week just completed.
    if profile == 'mon_fri' and day.weekday() > 4:
        return day - timedelta(days=day.weekday() - 4)
    return day + timedelta(days=(ending - day.weekday()) % 7)


def profile_start(ending, profile):
    return ending - timedelta(days=WORK_WEEKS[profile]['days'] - 1)


def saved_period(sheet):
    """Old sheets were seven days, including historical Friday-ending sheets."""
    sheet = dict(sheet)
    ending = date.fromisoformat(sheet['week_end'])
    start = date.fromisoformat(sheet['week_start']) if sheet.get('week_start') else ending - timedelta(days=6)
    profile = sheet.get('work_week')
    label = WORK_WEEKS[profile]['label'] if profile in WORK_WEEKS else (
        'Monday–Sunday' if ending.weekday() == 6 else 'Existing Friday-ending week')
    return start, ending, label
