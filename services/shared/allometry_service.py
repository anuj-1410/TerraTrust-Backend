"""Reviewed allometry primitives. No height/density/species fallback."""
from decimal import Decimal, localcontext

from models.institution.common import PI


def dbh(raw):
    form = raw["measurement_form"]
    if form == "DIAMETER":
        return Decimal(raw["dbh_cm"]) if raw["dbh_cm"] is not None else None
    if form == "CIRCUMFERENCE":
        with localcontext() as ctx:
            ctx.prec = 50
            return Decimal(raw["circumference_cm"]) / PI if raw["circumference_cm"] is not None else None
    return None


def chave_height(density, diameter_cm, height_m):
    if min(density, diameter_cm, height_m) <= 0:
        raise ValueError("Supported density, DBH and height must be positive.")
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal("0.0673") * ctx.power(density * diameter_cm ** 2 * height_m, Decimal("0.976"))


def tree_biomass(raw, parameters):
    equation = parameters.get(str(raw["allometry_id"]))
    density = parameters.get(str(raw["wood_density_source_id"]))
    if not equation or not density or equation["kind"] != "ALLOMETRY" or density["kind"] != "WOOD_DENSITY":
        raise ValueError("Supported allometry and density sources are required.")
    diameter = dbh(raw)
    if diameter is None or raw["height_m"] is None or density["value"] is None:
        raise ValueError("Actual dimensions and density are required.")
    rho = Decimal(str(density["value"]))
    if density["unit"] == "kg/m3":
        rho /= 1000
    elif density["unit"] != "g/cm3":
        raise ValueError("Unsupported wood-density unit.")
    if equation["formula_key"] != "CHAVE_2014_HEIGHT":
        raise ValueError("This allometry equation is not implemented.")
    return chave_height(rho, diameter, Decimal(raw["height_m"]))
