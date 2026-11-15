from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsd_exif import estimate_gsd_m, parse_xmp, gsd_from_image_bytes, sensor_width_for_model


def test_estimate_gsd_from_altitude_and_35mm():
    # 13 m AGL, 12 mm equiv on a 3840-wide frame is a toy number; check the formula.
    gsd = estimate_gsd_m(altitude_m=13.0, image_width_px=3840, focal_length_35mm=12.0)
    # 13 * 0.036 / (0.012 * 3840) = 0.01015625 m/px
    assert gsd is not None
    assert abs(gsd - 0.01015625) < 1e-9


def test_estimate_gsd_from_sensor_and_focal():
    # Avata 2-ish: 9.6 mm sensor, 6.0 mm focal, 13 m, 3840 px
    gsd = estimate_gsd_m(
        altitude_m=13.0, image_width_px=3840, focal_length_mm=6.0, sensor_width_mm=9.6
    )
    expected = 13.0 * 0.0096 / (0.006 * 3840)
    assert abs(gsd - expected) < 1e-12


def test_sensor_width_for_avata2():
    assert sensor_width_for_model("DJI", "Avata 2") == 9.6


def test_parse_dji_xmp_relative_altitude():
    xmp = b"""<?xpacket begin=''?>
    <x:xmpmeta xmlns:x='adobe:ns:meta/'>
      <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
        <rdf:Description
          xmlns:drone-dji='http://www.dji.com/drone-dji/1.0/'
          xmlns:exif='http://ns.adobe.com/exif/1.0/'
          drone-dji:RelativeAltitude='+13.00'
          exif:FocalLength='6.0'
          tiff:Make='DJI'
          tiff:Model='Avata 2'
          exif:FocalLengthIn35mmFilm='12'/>
      </rdf:RDF>
    </x:xmpmeta>
    <?xpacket end='w'?>"""
    # wrap in a pretend JPEG body
    data = b"\xff\xd8\xff\xe1" + xmp + b"\xff\xd9"
    info = parse_xmp(data)
    assert info["altitude_m"] == 13.0
    assert info["focal_length_mm"] == 6.0
    gsd = gsd_from_image_bytes(data, image_width_px=3840)
    assert gsd is not None
    # sensor 9.6 mm from model + 6.0 mm focal + 13 m
    expected = 13.0 * 0.0096 / (0.006 * 3840)
    assert abs(gsd - expected) < 1e-9


def test_parse_gimbal_pitch_and_focal_px():
    from gsd_exif import parse_xmp, focal_px_from_info
    xmp = b"""<?xpacket begin=''?>
    <x:xmpmeta xmlns:x='adobe:ns:meta/'>
      <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
        <rdf:Description
          xmlns:drone-dji='http://www.dji.com/drone-dji/1.0/'
          drone-dji:RelativeAltitude='+13.00'
          drone-dji:GimbalPitchDegree='-18.5'
          drone-dji:CalibratedFocalLength='2095.3'
          tiff:Make='DJI'
          tiff:Model='Avata 2'/>
      </rdf:RDF>
    </x:xmpmeta>
    <?xpacket end='w'?>"""
    info = parse_xmp(xmp)
    assert info["altitude_m"] == 13.0
    assert abs(info["gimbal_pitch_deg"] - (-18.5)) < 1e-6
    assert abs(info["focal_length_px"] - 2095.3) < 1e-6
    assert abs(focal_px_from_info(info, 3840) - 2095.3) < 1e-6
