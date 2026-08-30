# Screenshots

README embeds these images. Capture each from a live Home Assistant install and drop the PNG in this folder with the exact filename below. Until a file exists its README reference renders as a broken-image placeholder.

| File | Where it shows | What to capture |
|------|----------------|-----------------|
| `00_choose_entity.png` | Configuration → Step 1 | The config-flow first step: entity picker + optional name field. |
| `01_choose_mode.png` | Configuration → Step 2 | The mode menu (Specific states / All states). |
| `02_specific_states.png` | Configuration → Step 2a | States-to-track multi-select (prefilled, showing unavailable/unknown in the list) + enable-compliance toggle. |
| `03_compliance.png` | Configuration → Step 2a-i | Compliance step: target states + optional threshold. |
| `04_frames.png` | Configuration → Shared tail | Frame toggles + minimum-state-duration field. |
| `05_options.png` | Configuration → Options Flow | The Configure/options dialog for an existing tracker. |
| `06_specific_sensors.png` | Sensors → Specific-states mode | The device page listing duration sensors + currently_in_state + compliant, ideally with the compliant sensor's attributes expanded. |
| `07_allstates_sensor.png` | Sensors → All-states mode | A breakdown sensor with its attributes expanded (breakdown_seconds / breakdown_pct / counts / unaccounted_seconds). |
| `08_card_bars.png` | The Card | The custom card in `chart: bars` mode. |
| `09_card_pie.png` | The Card | The custom card in `chart: pie` mode (donut breakdown). |
| `10_card_table.png` | The Card | The custom card in `chart: table` mode. |

Keep filenames stable — the README links to them by name. PNG preferred; crop tight; light or dark theme is fine (be consistent).
