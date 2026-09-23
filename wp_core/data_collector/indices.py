"""Documented reflectance indices, computed per pixel before parcel aggregation."""
import numpy as np

BANDS = {"blue": 10, "green": 10, "red": 10, "nir": 10,
         "rededge1": 20, "rededge2": 20, "rededge3": 20, "nir08": 20,
         "swir16": 20, "swir22": 20, "scl": 20}
FORMULAS = {
    "ndvi": (10, "(B08-B04)/(B08+B04)"),
    "evi": (10, "2.5*(B08-B04)/(B08+6*B04-7.5*B02+1)"),
    "evi2": (10, "2.5*(B08-B04)/(B08+2.4*B04+1)"),
    "gndvi": (10, "(B08-B03)/(B08+B03)"),
    "savi": (10, "1.5*(B08-B04)/(B08+B04+0.5); L=0.5"),
    "msavi2": (10, "(2*B08+1-sqrt((2*B08+1)^2-8*(B08-B04)))/2"),
    "ndre": (20, "(B8A-B05)/(B8A+B05)"),
    "ci_rededge": (20, "B8A/B05-1"),
    "mtci": (20, "(B06-B05)/(B05-B04)"),
    "ireci": (20, "(B07-B04)/(B05/B06)"),
    "ndmi": (20, "(B08-B11)/(B08+B11)"),
    "msi": (20, "B11/B08"),
    "bsi": (20, "((B11+B04)-(B08+B02))/((B11+B04)+(B08+B02))"),
    "ndwi": (10, "(B03-B08)/(B03+B08)"),
    "mndwi": (20, "(B03-B11)/(B03+B11)"),
    "nbr": (20, "(B08-B12)/(B08+B12)"),
    "nbr2": (20, "(B11-B12)/(B11+B12)"),
    "psri": (20, "(B04-B02)/B06")
}
SOURCES = [
    "https://step.esa.int/main/wp-content/help/versions/13.0.0/snap-toolboxes/eu.esa.opt.opttbx.radiometric.indices.ui/OperatorsIndexList.html",
    "https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/",
    "https://www.usgs.gov/landsat-missions/landsat-enhanced-vegetation-index"
]


def ratio(a, b):
    a, b = np.broadcast_arrays(a, b)
    return np.divide(a, b, out=np.full(a.shape, np.nan, dtype=np.float32),
                     where=np.isfinite(a) & np.isfinite(b) & (np.abs(b) > 1e-6))


def calculate(bands, resolution):
    b, g, r, n = (bands[k] for k in ("blue", "green", "red", "nir"))
    if resolution == 10:
        radicand = (2*n+1)**2 - 8*(n-r)
        return {"ndvi": ratio(n-r, n+r), "evi": 2.5*ratio(n-r, n+6*r-7.5*b+1),
                "evi2": 2.5*ratio(n-r, n+2.4*r+1), "gndvi": ratio(n-g, n+g),
                "savi": 1.5*ratio(n-r, n+r+0.5),
                "msavi2": (2*n+1-np.sqrt(np.where(radicand >= 0, radicand, np.nan)))/2,
                "ndwi": ratio(g-n, g+n)}
    a, e1, e2, e3, s1, s2 = (bands[k] for k in
                              ("nir08", "rededge1", "rededge2", "rededge3", "swir16", "swir22"))
    return {"ndre": ratio(a-e1, a+e1), "ci_rededge": ratio(a, e1)-1,
            "mtci": ratio(e2-e1, e1-r), "ireci": ratio(e3-r, ratio(e1, e2)),
            "ndmi": ratio(n-s1, n+s1), "msi": ratio(s1, n),
            "bsi": ratio((s1+r)-(n+b), (s1+r)+(n+b)),
            "mndwi": ratio(g-s1, g+s1), "nbr": ratio(n-s2, n+s2),
            "nbr2": ratio(s1-s2, s1+s2), "psri": ratio(r-b, e2)}


def metadata():
    return {"formula_version": "sentinel2_sr_indices_v1", "unit": "dimensionless",
            "denominator_epsilon": 1e-6, "formulas": {
                k: {"resolution_m": v[0], "formula": v[1]} for k, v in FORMULAS.items()},
            "reflectance_units": "physical_surface_reflectance_not_DN",
            "resampling": "10m reflectance averaged to 20m; no invented 10m red-edge detail",
            "sources": SOURCES, "clipping": "none; undefined values remain null",
            "aggregation": "exact pixel-parcel intersection area weighted mean/std/CDF quantiles",
            "warning": "Indices are correlated observations, not independent crop or irrigation evidence"}
