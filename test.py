import openhtf as htf
from openhtf.output.servers import station_server
from openhtf.output.web_gui import web_launcher
from openhtf.plugs import user_input
from openhtf.util import configuration
from openhtf.plugs.user_input import UserInput
import serial
import pyocd
import time

CONF = configuration.CONF

# -----------------------------
# Plugs: Hardware Interfaces
# -----------------------------
class StlinkPlug(htf.BasePlug):
    def open(self):
        self.loader = pyocd.FlashLoader(target='stm32f411ce')
    def flash(self, path):
        self.loader.flash(path)
    def reset(self):
        self.loader.reset()
    def close(self):
        self.loader.close()

class UartPlug(htf.BasePlug):
    def __init__(self, port='/dev/ttyUSB0'):
        self.port = port
    def open(self):
        self.ser = serial.Serial(self.port, 115200, timeout=2)
    def cmd(self, command, expect=None):
        self.ser.write(f"{command}\r\n".encode())
        resp = self.ser.read_until(b'\r\n').decode().strip()
        if expect and expect not in resp:
            raise RuntimeError(f"Expected '{expect}', got '{resp}'")
        return resp
    def close(self):
        self.ser.close()

# -----------------------------
# Test Phases
# -----------------------------

@htf.plug(user_input=UserInput)
def start_dut(test, user_input):
    dut_id = user_input.prompt(
        message="Scan DUT barcode (SN):",
        text_input=True
    )
    test.dut_id = dut_id
    user_input.prompt(
        message=f"DUT {dut_id} → Plug into fixture and click OK",
        text_input=False
    )

@htf.plug(stlink=StlinkPlug)
def flash_firmware(test, stlink):
    test.logger.info("Flashing test firmware...")
    stlink.flash("firmware/io_test_mode.hex")
    stlink.reset()
    test.measurements['flash_status'] = 'OK'

@htf.plug(uart=UartPlug)
def uart_handshake(test, uart):
    resp = uart.cmd("PING", expect="PONG")
    test.measurements['uart'] = resp

@htf.plug(uart=UartPlug, user_input=UserInput)
def test_input_channels(test, uart, user_input):
    for i in range(8):
        user_input.prompt(
            message=f"Apply 24V to IN{i} → Click OK when done",
            text_input=False
        )
        state = int(uart.cmd(f"IN{i}"))
        test.measurements[f'input_{i}'] = state
        if state != 1:
            raise htf.PhaseError(f"IN{i} not detected!")

        user_input.prompt(
            message=f"Remove 24V from IN{i} → Click OK",
            text_input=False
        )
        state = int(uart.cmd(f"IN{i}"))
        if state != 0:
            raise htf.PhaseError(f"IN{i} leakage!")

@htf.plug(uart=UartPlug, user_input=UserInput)
def test_output_channels(test, uart, user_input):
    for i in range(8):
        uart.cmd(f"OUT{i}1")
        current_str = user_input.prompt(
            message=f"Measure current on IO{i} (mA):",
            text_input=True
        )
        try:
            current = float(current_str)
        except:
            raise htf.PhaseError("Invalid current value")
        test.measurements[f'io{i}_current_mA'] = current
        if not (50 < current < 500):
            raise htf.PhaseError(f"IO{i} current out of range: {current}mA")
        uart.cmd(f"OUT{i}0")

@htf.plug(uart=UartPlug)
def rs485_loopback(test, uart):
    uart.cmd("RS485 TEST123")
    resp = uart.cmd("RS485?")
    if "TEST123" not in resp:
        raise htf.PhaseError("RS-485 loopback failed")
    test.measurements['rs485'] = 'PASS'

@htf.plug(uart=UartPlug, user_input=UserInput)
def led_visual_check(test, uart, user_input):
    uart.cmd("LED 1")
    ok = user_input.prompt(
        message="Is LED1 ON? (Yes/No)",
        text_input=True
    )
    if ok.strip().lower() != "yes":
        raise htf.PhaseError("LED1 not visible")
    uart.cmd("LED 0")
    test.measurements['led1'] = 'visible'

@htf.plug(uart=UartPlug)
@htf.measures(htf.Measurement('vdd_3v3').in_range(3.15, 3.45).with_units('V'))
def measure_3v3(test, uart):
    v = float(uart.cmd("VOLT?"))
    test.measurements.vdd_3v3 = v

# -----------------------------
# Main Entry
# -----------------------------
def main():
    CONF.load(station_server_port=4444)
    
    with station_server.StationServer() as server:
        # Launch browser
        web_launcher.launch("http://localhost:4444")
        
        test = htf.Test(
            start_dut,
            flash_firmware,
            uart_handshake,
            measure_3v3,
            test_input_channels,
            test_output_channels,
            rs485_loopback,
            led_visual_check,
        )
        
        # Publish to Web GUI
        test.add_output_callbacks(server.publish_final_state)
        
        # Start test when operator clicks "Start" in GUI
        test.execute(test_start=user_input.prompt_for_test_start(
            message="Click START to begin IO-CONTROLLER test"
        ))

if __name__ == '__main__':
    main()