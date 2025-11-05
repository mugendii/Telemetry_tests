import openhtf as htf
from openhtf.output.servers import station_server
from openhtf.output.web_gui import web_launcher
from openhtf.plugs import user_input
from openhtf.util import configuration
import serial
import pyocd
import time
import pathlib
import re

CONF = configuration.CONF


class StlinkPlug(htf.BasePlug):
    def open(self):
        self.loader = pyocd.FlashLoader(target='stm32l475vg')
        self.loader.open()
    def flash(self, hex_path: str):
        self.loader.erase_all()
        self.loader.flash(hex_path)
        self.loader.verify(hex_path)
    def reset(self):
        self.loader.reset()
    def close(self):
        self.loader.close()

class UartPlug(htf.BasePlug):
    def __init__(self, port='/dev/ttyUSB0', baud=115200):
        self.port = port
        self.baud = baud
    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=2)
    def cmd(self, command: str, expect: str | None = None) -> str:
        self.ser.flushInput()
        self.ser.write(f"{command}\r\n".encode())
        resp = self.ser.read_until(b'\r\n').decode().strip()
        if expect and expect not in resp:
            raise RuntimeError(f"Expected '{expect}' → got '{resp}'")
        return resp
    def close(self):
        self.ser.close()

# NEW: Dedicated COM4 plug for temperature
class TempUartPlug(htf.BasePlug):
    def open(self):
        self.ser = serial.Serial('COM4', 115200, timeout=5)
        self.ser.flushInput()
    def read_temperature_line(self) -> str:
        start = time.time()
        while True:
            line = self.ser.readline().decode('utf-8', errors='ignore').strip()
            if line.startswith('TEMPERATURE ='):
                return line
            if time.time() - start > 10:
                raise RuntimeError("Timeout waiting for TEMPERATURE line")
    def close(self):
        self.ser.close()

# ------------------------------------------------------------------
# Phases
# ------------------------------------------------------------------
@htf.plug(user_input=user_input.UserInput)
def start_dut(test, user_input):
    dut_id = "AUTO_" + time.strftime("%H%M%S")
    test.dut_id = dut_id
    test.logger.info(f"Auto DUT ID: {dut_id}")
@htf.plug(stlink=StlinkPlug)
def flash_firmware(test, stlink):
    test.logger.info("Flashing NEW firmware …")
    new_hex = str(pathlib.Path(__file__).parent / "firmware" / "L4_temp_sensor.hex")
    stlink.flash(new_hex)
    stlink.reset()
    test.measurements['flash_status'] = 'OK'

@htf.plug(uart=UartPlug)
def verify_version(test, uart):
    version = uart.cmd("VERSION", expect="IOCTRL_V2")
    test.measurements['firmware_version'] = version

@htf.plug(uart=UartPlug)
def uart_handshake(test, uart):
    resp = uart.cmd("PING", expect="PONG")
    test.measurements['uart_handshake'] = resp

@htf.plug(uart=UartPlug)
@htf.measures(htf.Measurement('vdd_3v3').in_range(3.15, 3.45).with_units('V'))
def measure_3v3(test, uart):
    v = float(uart.cmd("VOLT?"))
    test.measurements.vdd_3v3 = v

# NEW PHASE
@htf.plug(temp_uart=TempUartPlug)
@htf.measures(
    htf.Measurement('board_temperature')
        .in_range(15.0, 45.0)
        .with_units('°C')
)
def get_temperature(test, temp_uart):
    line = temp_uart.read_temperature_line()
    test.logger.info(f"Raw temp line: {line}")
    m = re.search(r'TEMPERATURE\s*=\s*([0-9.]+)', line)
    if not m:
        raise htf.PhaseError(f"Could not parse temperature from: {line}")
    temp_c = float(m.group(1))
    test.measurements.board_temperature = temp_c
    test.logger.info(f"Temperature = {temp_c:.2f}°C → PASS")

@htf.plug(uart=UartPlug, user_input=user_input.UserInput)
def led_visual_check(test, uart, user_input):
    uart.cmd("LED 1")
    ok = user_input.prompt(message="Is LED1 ON? (Yes/No)", text_input=True)
    if ok.strip().lower() != "yes":
        raise htf.PhaseError("LED1 not visible")
    uart.cmd("LED 0")
    test.measurements['led1'] = 'visible'

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    CONF.load(
        station_server_port=4444,
        test_timeout_ms=300_000
    )
    with station_server.StationServer() as server:
        web_launcher.launch("http://localhost:4444")
        test = htf.Test(
            start_dut,
            flash_firmware,
            verify_version,
            uart_handshake,
            measure_3v3,
            get_temperature,      # ← NEW
            led_visual_check,
        )
        test.add_output_callbacks(server.publish_final_state)
        test.execute(
            test_start=user_input.prompt_for_test_start(
                message="Click START to begin IO-CONTROLLER flash & test"
            )
        )

if __name__ == '__main__':
    main()