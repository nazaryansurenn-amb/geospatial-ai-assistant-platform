"""One sustained transition in preserved 2021-2025 seasonal EO states."""
import json

YEARS = (2021, 2022, 2023, 2024, 2025)
CLASSES = ("became_active", "became_inactive")


def transition(codes, profile_year_count, household=False, road=False):
    if household or road:
        return "excluded", None
    if isinstance(codes, str):
        try:
            codes = json.loads(codes)
        except (ValueError, TypeError):
            return "insufficient", None
    if not isinstance(codes, (list, tuple)) or len(codes) != 5:
        return "insufficient", None
    if any(type(code) is not int or code not in (1, 2, 3) for code in codes):
        return "insufficient", None
    if profile_year_count != 5:
        return "insufficient", None
    active = [code in (1, 2) for code in codes]
    switches = [i for i in range(1, len(YEARS)) if active[i] != active[i - 1]]
    if len(switches) != 1:
        return "not_selected", None
    return ("became_active" if active[-1] else "became_inactive"), YEARS[switches[0]]


def classify(codes, profile_year_count, household=False, road=False):
    return transition(codes, profile_year_count, household, road)[0]


def summary(frame):
    def total(selected):
        return {"parcel_count": int(len(selected)),
                "area_ha": round(float(selected.area_official_m2.sum()) / 10000, 6)}
    result = {"classes": {key: total(frame[frame.change_class.eq(key)]) for key in CLASSES}}
    for key in ("insufficient", "excluded", "not_selected"):
        result[key] = total(frame[frame.change_class.eq(key)])
    result["all"] = total(frame)
    result["by_change_year"] = {
        str(year): {key: total(frame[frame.change_class.eq(key) & frame.change_year.eq(year)]) for key in CLASSES}
        for year in YEARS[1:]
    }
    # Difference in whole cadastral hectares, not a measurement of cropped area.
    result["net_parcel_area_change_ha"] = round(result["classes"]["became_active"]["area_ha"] - result["classes"]["became_inactive"]["area_ha"], 6)
    return result
