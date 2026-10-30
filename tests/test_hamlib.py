"""Hamlib's radio list: parsed from `rigctl -l` as the card prints it."""

from vector_bridge import hamlib

# Cut from `rigctl -l` on the bench card (Hamlib 4.6.2), with its fixed-width columns intact.
LIST = """\
 Rig #  Mfg                    Model                   Version         Status      Macro
     1  Hamlib                 Dummy                   20240709.0      Stable      RIG_MODEL_DUMMY
     2  Hamlib                 NET rigctl              20240418.0      Stable      RIG_MODEL_NETRIGCTL
     4  FLRig                                          20250107.0      Stable      RIG_MODEL_FLRIG
  1036  Yaesu                  FT-891                  20241118.11     Stable      RIG_MODEL_FT891
  2014  Kenwood                TS-2000                 20250107.2      Stable      RIG_MODEL_TS2000
  3073  Icom                   IC-7300                 20250107.14     Stable      RIG_MODEL_IC7300
  3085  Icom                   IC-705                  20250107.11     Stable      RIG_MODEL_IC705
"""


def test_the_list_keeps_radios_with_their_maker_and_drops_hamlibs_own_entries():
    models = hamlib.parse_list(LIST)
    assert [m["model"] for m in models] == [4, 3085, 3073, 2014, 1036]  # by maker, then name
    assert {"model": 3073, "mfg": "Icom", "name": "IC-7300", "status": "Stable"} in models
    assert next(m for m in models if m["model"] == 4)["name"] == "FLRig"  # no model name: the maker stands in


def test_output_without_the_header_gives_no_radios():
    assert hamlib.parse_list("") == [] and hamlib.parse_list("rigctl: command not found\n") == []


# Rows from `rigctl -l` of Hamlib 4.7.2 (trixie-backports, run on the bench card 2026-10-04). 4.7 moved
# the Guohe rigs to five-digit numbers and gave 1051 to the Yaesu FTX-1.
LIST_472 = """\
 Rig #  Mfg                    Model                   Version         Status      Macro
     1  Hamlib                 Dummy                   20240709.0      Stable      RIG_MODEL_DUMMY
     2  Hamlib                 NET rigctl              20250211.0      Stable      RIG_MODEL_NETRIGCTL
  1051  Yaesu                  FTX-1                   20251224.0      Beta        RIG_MODEL_FTX1
 40001  GUOHETEC               PMR-171                 20250611.0      Stable      RIG_MODEL_PMR171
 40002  GUOHETEC               Q900                    20250611.0      Stable      RIG_MODEL_Q900
"""


def test_hamlib_4_7_rows_and_five_digit_models_parse():
    models = {m["model"]: m for m in hamlib.parse_list(LIST_472)}
    assert set(models) == {1051, 40001, 40002}
    assert models[1051] == {"model": 1051, "mfg": "Yaesu", "name": "FTX-1", "status": "Beta"}
    assert models[40002] == {"model": 40002, "mfg": "GUOHETEC", "name": "Q900", "status": "Stable"}


# Cut from `rigctl -m 2014 -L` (Hamlib 4.6.5), tabs intact; one name has spaces in it.
CONF = """\
model: TS-2000
post_write_delay: "Delay in ms between each command sent out"
\tDefault: 0, Value: 0
\tRange: 0..1000, step 1.0
Selected range list: "The tx/rx range list in use"
\tDefault: 0, Value: 
\tRange: 1..5, step 1.0
device_id: "User-specified device ID for multicast state data and commands"
\tDefault: , Value: 
\tString.
auto_power_on: "True enables compatible rigs to be powered up on open"
\tDefault: 0, Value: 0
\tCheck button.
serial_handshake: "Serial port handshake"
\tDefault: None, Value: Hardware
\tCombo: None, XONXOFF, Hardware
rts_state: "Serial port set state of RTS signal for external powering"
\tDefault: Unset, Value: Unset
\tCombo: Unset, ON, OFF
"""


def test_conf_is_parsed_and_names_with_spaces_are_left_out():
    ps = {p["name"]: p for p in hamlib.parse_conf(CONF)}
    assert list(ps) == ["post_write_delay", "device_id", "auto_power_on", "serial_handshake", "rts_state"]
    assert ps["post_write_delay"] | {} == {"name": "post_write_delay", "desc": "Delay in ms between each command sent out",
                                         "default": "0", "kind": "range", "min": 0.0, "max": 1000.0}
    assert ps["serial_handshake"]["options"] == ["None", "XONXOFF", "Hardware"]
    assert ps["auto_power_on"]["options"] == ["0", "1"] and ps["device_id"]["kind"] == "string"


def test_expert_settings_are_checked_against_the_radios_own_list():
    ps = hamlib.parse_conf(CONF)
    assert hamlib.check_conf(" rts_state=OFF ; post_write_delay = 50,", ps) == "rts_state=OFF,post_write_delay=50"
    assert hamlib.check_conf("", ps) == ""
    for bad, why in (("serial_speed=9600", "baud rate"), ("ptt_pathname=/dev/x", "x"), ("tcp_pathname=x", "another device"),
                     ("nope=1", "no setting"), ("rts_state=MAYBE", "one of"), ("post_write_delay=5000", "0 to 1000"),
                     ("post_write_delay=abc", "number"), ("rts_state=ON,rts_state=OFF", "twice"), ("rts_state", "name=value"),
                     ("device_id=a b", "name=value"), ("multicast_cmd_addr=1.2.3.4", "safety proxy")):
        try:
            hamlib.check_conf(bad, ps)
        except ValueError as e:
            assert why in str(e), (bad, str(e))
        else:
            raise AssertionError(f"accepted {bad!r}")
    assert hamlib.check_conf("anything=1", []) == "anything=1"  # no list read: only the shape is checked
