"""Strict 2025 changes in preserved seasonal EO states, not legal land-use facts."""
import json

YEARS = (2021, 2022, 2023, 2024, 2025)
CLASSES = ("became_active", "became_inactive")


def classify(codes, profile_year_count, household=False, road=False):
    if household or road:
        return "excluded"
    if isinstance(codes, str):
        try:
            codes = json.loads(codes)
        except (ValueError, TypeError):
            return "insufficient"
    if not isinstance(codes, (list, tuple)) or len(codes) != 5:
        return "insufficient"
    if any(type(code) is not int or code not in (1, 2, 3) for code in codes):
        return "insufficient"
    if profile_year_count != 5:
        return "insufficient"
    if all(code == 3 for code in codes[:4]) and codes[4] in (1, 2):
        return "became_active"
    if all(code in (1, 2) for code in codes[:4]) and codes[4] == 3:
        return "became_inactive"
    return "not_selected"


def summary(frame):
    def total(selected):
        return {"parcel_count": int(len(selected)),
                "area_ha": round(float(selected.area_official_m2.sum()) / 10000, 6)}
    result = {"classes": {key: total(frame[frame.change_class.eq(key)]) for key in CLASSES}}
    for key in ("insufficient", "excluded", "not_selected"):
        result[key] = total(frame[frame.change_class.eq(key)])
    result["all"] = total(frame)
    # Difference in whole cadastral hectares, not a measurement of cropped area.
    result["net_parcel_area_change_ha"] = round(result["classes"]["became_active"]["area_ha"] - result["classes"]["became_inactive"]["area_ha"], 6)
    return result
