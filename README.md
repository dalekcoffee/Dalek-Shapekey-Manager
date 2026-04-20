# Dalek's Shapekey Manager

A Blender 5.0 add-on for previewing, filtering, navigating, and managing large numbers of shape keys with speed and clarity.

---

## Features

### Category System

Shape keys named with surrounding tokens (like `===Face===` or `---Brows---`) are treated as **category dividers**. They appear as section headers in the list, are never selected, never previewed, and never modified.

- Top-level categories use `===TOKEN===`
- Sub-level categories use `---TOKEN---`
- A **Category** dropdown appears automatically when dividers are detected, letting you filter the list to a single category or view all at once
- Category dividers from any model maker are supported -- the tokens are fully configurable in the Configuration panel

### Live Preview

- Drag the **Preview Value** slider to apply a shape key to the mesh in real time, no operator required
- **Auto Reset Others** zeros out all other shape keys before previewing so you see each key in isolation
- **Auto Preview on Select** automatically triggers a preview whenever you click a key in the list
- **Reset All to 0** resets every shape key at once with one click

### Step Navigation

- **Prev / Next** buttons step through the filtered list in order
- **Enable Arrow Keys** activates a modal that lets you use Up/Down arrow keys to walk through keys while working elsewhere in the UI -- press ESC or click the button again to deactivate
- The current position and total count are shown at all times (e.g. `VRC_Eye_Left  (12 / 84)`)

### Filter and Sort

- **Text search** filters keys by name in real time (case-insensitive)
- **Sort modes**: Default order, A to Z, Z to A, By Value (highest first), Non-zero first
- **Show Basis** toggle includes or hides the Basis key
- **Highlight Active** marks keys with a value above 0 with a distinct icon and colored value readout

### Pagination

- Configurable per-page count (5 to 100 keys per page)
- First / Prev / Next / Last page controls
- Shows the current range (e.g. `Showing 1-50 of 312`)

### Per-Key Actions

Every shape key row has quick-access buttons:

| Button | Action |
|--------|--------|
| Play | Preview this key at the current preview value |
| Copy | Copy the key name to the clipboard |
| Checkmark | Apply the key (bake its effect into the mesh and remove it) |
| X | Delete the key (with confirmation) |

### Delete Category

When a specific category is selected, a **Delete Category** button appears showing the exact count of keys that will be removed. Clicking it opens a confirmation dialog with:

- A 5-second cooldown before the OK button becomes active (configurable)
- A scrollable, searchable preview list of every key that will be deleted
- Deletion of the category divider itself along with all member keys

### Configuration Panel

A **Configuration** sub-panel lives at the bottom of the add-on. Expand it to access:

**Category Divider Patterns**

An editable list of token patterns that identify category dividers. Pre-populated with:

- `===` -- Top-level category header
- `---` -- Sub-level category header

Add as many custom tokens as you need to match whatever naming convention your model uses. Remove or reset to defaults at any time.

**Other Settings**

- **Delete Cooldown** -- How long (in seconds) the confirm button is locked after opening the delete category dialog. Defaults to 5 seconds, adjustable from 0 to 30.

---

## Installation

1. Download the latest `.zip` from the [Releases](../../releases) page
2. In Blender, go to **Edit > Preferences > Add-ons > Install**
3. Select the downloaded `.zip` file
4. Enable the add-on by checking the box next to **Dalek's Shapekey Manager**

The panel appears in **Properties > Object Data Properties > Shape Keys > Dalek's Shapekey Manager** when a mesh with shape keys is selected.

> Blender 5.0 or later is required.

---

## Screenshots

![Main Panel](Screenshot%201.png)
![Category Filtering](Screenshot%202.png)
![Configuration Panel](Screenshot%203.png)

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for details.
