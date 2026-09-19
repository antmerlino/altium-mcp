"""CubeMX <-> Altium MCU pinout tooling.

Pure Python, no Altium required. Layer 1 of the design-automation stack:

    ioc      - read and MODIFY a CubeMX .ioc without disturbing the rest of it
    mcu_db   - read a CubeMX MCU database XML (every pin, every alternate signal)
    model    - the canonical Pinout: MCU DB + .ioc + the designer's own columns,
               round-tripped through JSON and (optionally) a spreadsheet
    naming   - the house rules for pin names, net labels, symbol names, types
    layout   - a Pinout -> an Altium symbol spec, in the exact pipe-string
               format create_schematic_symbol consumes
    preview  - render a symbol spec to SVG so it can be inspected before any
               Altium call is made

Everything here is testable against synthetic fixtures under tests/fixtures.
"""
