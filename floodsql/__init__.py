"""Spatial-SQL screen of critical facilities in FEMA flood hazard areas (Texas Gulf Coast)."""
import os as _os
import sys as _sys

# Windows + conda: MKL can crash on its default threading layer, and DLLs (GDAL,
# PROJ, GEOS) resolve wrongly when the env is not activated. Fix both before any
# geospatial import.
_os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
if _sys.platform == "win32":
    _bin = _os.path.join(_sys.prefix, "Library", "bin")
    if _os.path.isdir(_bin) and not _os.environ.get("PATH", "").lower().startswith(_bin.lower()):
        _os.environ["PATH"] = _bin + _os.pathsep + _os.environ.get("PATH", "")
    for _var, _sub in (("GDAL_DATA", "gdal"), ("PROJ_DATA", "proj")):
        _dir = _os.path.join(_sys.prefix, "Library", "share", _sub)
        if _os.path.isdir(_dir):
            _os.environ.setdefault(_var, _dir)

__version__ = "0.1.0"

# Study counties: FIPS -> name. Jefferson (48245) has no effective digital NFHL
# county product on the FEMA Map Service Center, so it is not in the default set.
COUNTIES = {
    "48201": "Harris",
    "48167": "Galveston",
    "48039": "Brazoria",
    "48157": "Fort Bend",
    "48355": "Nueces",
}

FACILITY_TYPES = ("hospital", "nursing_home", "public_school", "fire_ems")
