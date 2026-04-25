---

id: iot
title: IoT Module
sidebar_position: 18
format: md
---




# iot

GPIO and IoT device control for Raspberry Pi and compatible hardware. Provides digital read/write, PWM output, analog operations, and edge-detection event callbacks.

| Property | Value |
|----------|-------|
| **Module ID** | `iot` |
| **Version** | `0.1.0` |
| **Type** | hardware |
| **Platforms** | Raspberry Pi, Linux |
| **Dependencies** | `RPi.GPIO` (optional — falls back to MockGPIO) |
| **Declared Permissions** | `gpio.read`, `gpio.write`, `actuator` |

---


## Actions (10)

### Pin Configuration

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `set_pin_mode` | Configure pin as input or output | `pin`, `mode` (`input`/`output`), `pull` (`none`/`up`/`down`) |
| `get_pin_state` | Get state of all configured pins | |
| `cleanup` | Release all GPIO resources | |

**Security for set_pin_mode**: `@requires_permission(Permission.GPIO_WRITE)`

### Digital I/O

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `digital_read` | Read pin value (0 or 1) | `pin` |
| `digital_write` | Write HIGH or LOW | `pin`, `value` (0 or 1) |

**Security**:
- `digital_read`: `@requires_permission(Permission.GPIO_READ)`
- `digital_write`: `@requires_permission(Permission.GPIO_WRITE)`, `@audit_trail("standard")`

### PWM Control

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `pwm_start` | Start PWM signal | `pin`, `frequency` (Hz), `duty_cycle` (0-100%) |
| `pwm_set_duty_cycle` | Update duty cycle | `pin`, `duty_cycle` |
| `pwm_stop` | Stop PWM channel | `pin` |

**Security**:
- All PWM actions: `@requires_permission(Permission.ACTUATOR)`
- `pwm_start`, `pwm_stop`: `@audit_trail("standard")`

### Event Detection

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `add_event_detect` | Register edge-detection callback | `pin`, `edge` (`rising`/`falling`/`both`), `callback_plan` |
| `remove_event_detect` | Remove edge detection | `pin` |

Edge detection allows the IoT module to fire plans when a sensor state changes, integrating with the trigger system.

---


## GPIO Backend Architecture

```
IoTModule
    |
    v
GPIOInterface (ABC)
    |
    +--→ RaspberryPiGPIO  — Uses RPi.GPIO library
    |
    +--→ MockGPIO          — In-memory simulation for development/testing
```

The backend is automatically selected based on platform:
- Raspberry Pi with `RPi.GPIO` installed: `RaspberryPiGPIO`
- Everything else: `MockGPIO` (logs operations, simulates pin states)

For testing, the backend can be injected via constructor.

---


## Implementation Notes

- BCM pin numbering (GPIO numbers, not physical pin numbers)
- PinMode enum: `INPUT`, `OUTPUT`
- PullResistor enum: `NONE`, `UP`, `DOWN`
- EdgeType enum: `RISING`, `FALLING`, `BOTH`
- Thread-safe pin state tracking
- Cleanup on module stop (releases all GPIO resources)
