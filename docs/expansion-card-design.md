# Framework Desktop OLED Tile & Custom Multi-I/O Expansion Card

## 1. OLED Tile Connection Options
Because the Framework Desktop's front tiles are passive magnetic plastic pieces without built-in power or data traces, connecting an OLED display requires routing power and signals from an available port or motherboard header:

| Method | Data + Power | Preserves Front Slots? | Wiring Visibility | Best For |
| :--- | :--- | :--- | :--- | :--- |
| **Front USB-C Expansion Card** | Both | No (uses 1 slot) | Short external loop | Quick prototyping & easy flashing |
| **Internal USB Type-E Header** | Both | Yes | Hidden inside case | Cleanest stealth wired OLED build |
| **Rear USB4 / HDMI** | Both | Yes | Routed along chassis | Mini secondary OS monitor (AIDA64/Rainmeter) |
| **Wi-Fi / BLE + Internal 5V Tap** | Wireless + 5V tap | Yes | 2 thin power wires | Zero data cabling; standalone telemetry |
| **Custom I/O Expansion Card** | Both + Pass-through | Partial (retains USB-A/C) | Ultra-thin internal Qwiic ribbon | Integrated OLED + debug/serial bench tool |

---

## 2. Custom Multi-I/O Expansion Card Architecture
To combine a front-facing **USB-A port**, **Dual Qwiic (I2C)** headers for the OLED tile, **UART**, and a **CMSIS-DAP (SWD)** debug probe on a single Framework Expansion Card (~30 mm × 32 mm PCB), use an **RP2040** microcontroller behind a **2-port USB 2.0 hub**.

*(Note: Fixed-function USB bridges like the MCP2221A cannot run CMSIS-DAP firmware, and passive USB-C splits break 40Gbps USB4 signal integrity—making the USB 2.0 Hub + RP2040 design the most reliable approach.)*

### Block Diagram
```text
From Framework Slot (USB 2.0 D+/D-, VBUS, GND)
                 │
                 ▼
      [ USB 2.0 Hub IC ] (Microchip USB2512B or Terminus FE1.1s)
       ├── Downstream Port 1 ──> [ Female USB-A Port ] (Protected via 5V power switch)
       └── Downstream Port 2 ──> [ RP2040 Microcontroller ] (Composite USB Firmware)
                                  ├── CMSIS-DAP (SWD / SWCLK) ──> 6-Pin Debug Header
                                  ├── UART (TX / RX)          ──> 6-Pin Debug Header
                                  └── I2C (SDA / SCL)         ──> Dual Qwiic (Top & Front)
```

---

## 3. Key Hardware & Pinout Specifications

### Front-End USB & Power
* **Upstream USB-C Plug:** Connect `D+`, `D-`, `VBUS`, and `GND` to the USB 2.0 Hub IC. Place **5.1kΩ pull-down resistors (`Rd`)** on `CC1` and `CC2` so the host supplies 5V VBUS.
* **Downstream USB-A Port:** Route Hub Port 1 to a low-profile SMD USB-A receptacle, protected by a 5V current-limiting switch (e.g., **TPS2051** or **MIC2025**).
* **Downstream RP2040:** Route Hub Port 2 to `USB_D+` / `USB_D-` via **27Ω** series termination resistors.
* **RP2040 Support:** Include an **AP2112K-3.3** LDO regulator, a **12 MHz crystal** (with 15pF load caps), and a **W25Q128JVSIQ** (16MB QSPI Flash).

### Dual Qwiic / STEMMA QT Bus (4-Pin JST-SH 1.0mm)
Wire **two 4-pin JST-SH connectors** in parallel (`GND`, `3.3V`, `SDA`, `SCL`) using `GPIO 4` (`SDA`) and `GPIO 5` (`SCL`), with **one pair of 4.7kΩ pull-ups** to 3.3V on the PCB:
* **Top Edge Connector:** Routes a flat/silicone Qwiic cable behind the magnetic front bezel directly to the OLED tile.
* **Front Face Connector:** Exposed for hot-plugging external I2C sensors or displays.

### 6-Pin FTDI-Style UART & CMSIS-DAP Header
| Pin | Signal | RP2040 GPIO | Function |
| :---: | :--- | :--- | :--- |
| **1** | `GND` | `GND` | Common ground |
| **2** | `SWDIO` | `GPIO 2` | CMSIS-DAP Debug Data (SWD) |
| **3** | `3.3V` | `3.3V` Out | Regulated 3.3V power output |
| **4** | `TX` | `GPIO 0` (`UART0 TX`) | Serial transmit (connects to target `RX`) |
| **5** | `RX` | `GPIO 1` (`UART0 RX`) | Serial receive (connects to target `TX`) |
| **6** | `SWCLK` | `GPIO 3` | CMSIS-DAP Debug Clock (SWD) |

---

## 4. Sourcing & Reference Files
* **PCB Template:** Clone `FrameworkComputer/ExpansionCards` on GitHub for the official KiCad mechanical outline and USB-C male edge footprint.
* **Cables:** Search for **50 mm or 100 mm Flexible Silicone Qwiic / STEMMA QT 4-pin JST-SH cables** (e.g., SparkFun `PRT-17260` / `PRT-17259` or Adafruit `4399` via Mouser, DigiKey, Pimoroni, or BerryBase) so the wire bends flat behind the magnetic tiles without pushing them outward.
* **Firmware:** Flash Raspberry Pi's open-source **`debugprobe`** firmware (or a TinyUSB composite build) onto the RP2040 to expose CMSIS-DAP, USB CDC UART, and USB-to-I2C simultaneously.