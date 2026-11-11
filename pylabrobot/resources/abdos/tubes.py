"""Resource definitions for Abdos tubes."""

from pylabrobot.resources.tube import Tube


def abdos_tube_2mL_skirted(name: str) -> Tube:
  """Abdos P10235: 2 mL self-standing, skirted screw-cap tube, without its cap.

  Polypropylene, nonsterile, supplied unassembled with a natural-colour cap.
  Outer diameter and nominal capacity are from the P10235 ordering-table row on page 3 of:
  https://www.abdoslifesciences.com/wp-content/uploads/2025/11/ABDOS-Screw-Cap-Tubes.pdf
  Archived source: ``technical_drawings/ABDOS-Screw-Cap-Tubes.pdf``.

  The page 2 drawing includes the cap (13.0 mm diameter, 46.7 mm overall height);
  these are not the tube-body dimensions used here. The uncapped external height
  uses the operator-confirmed value of 44.70 mm. The modeled skirt-base-to-cavity-bottom
  offset is 0.25 mm, giving a rim-to-cavity-bottom depth of 44.70 - 0.25 = 44.45 mm.

  The measured opening inner diameter is 8.35 mm, recorded in
  ``metadata["opening_inner_diameter"]`` in mm. The measured 0.85 mm sidewall
  thickness is recorded separately in ``metadata["sidewall_thickness"]`` in mm;
  it is not the vertical base offset. The opening measurement is independent of
  the sidewall measurement. Internal taper and V-bottom dimensions are not
  characterized, so volume/height conversions remain unset. Seating height
  depends on the carrier and insert.

  Args:
    name: Name of the tube.
  """
  diameter = 10.15  # from spec: ordering-table outer diameter, not inner diameter
  tube = Tube(
    name=name,
    size_x=diameter,
    size_y=diameter,
    size_z=44.70,  # operator-confirmed uncapped overall height
    max_volume=2_000,  # from spec: nominal capacity in uL
    material_z_thickness=0.25,  # modeled vertical skirt-base-to-cavity-bottom offset
    model=abdos_tube_2mL_skirted.__name__,
  )
  tube.metadata["opening_inner_diameter"] = 8.35  # measured at the rim, in mm
  tube.metadata["sidewall_thickness"] = 0.85  # measured sidewall thickness, in mm
  return tube
