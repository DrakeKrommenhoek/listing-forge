# Photography and file organisation

Photographs are the single biggest lever on both identification accuracy and
sale price. Everything else in the pipeline is downstream of them.

## Directory standard

Every item gets the same tree, created automatically the moment an item ID is
allocated:

```
inventory/<ITEM_ID>/
    original/     untouched photos exactly as received from the phone
    web/          resized, EXIF-stripped images for the catalogue site
    listing/      marketplace-sized images
    research/     comps worksheet and research notes
    copy/         generated listing packages (one .md per platform + one .json)
    approval/     approval record: who approved what, when, at what price
```

`original/` is never published. Phone photos carry GPS coordinates; publishing
one can publish the house address. Only `web/` and `listing/` derivatives —
which are re-encoded from pixel data and therefore carry no metadata — are ever
served or uploaded.

## Filename convention

```
<ITEM_ID>_<NN>_<slot>.<ext>

DK-202608-014_01_hero.jpg
DK-202608-014_02_front.jpg
DK-202608-014_07_defect.jpg
```

- `NN` is a zero-padded sequence in capture order.
- `slot` is one of: `hero`, `front`, `back`, `side-l`, `side-r`, `top`,
  `bottom`, `label`, `serial`, `accessories`, `dimensions`, `defect`, `detail`,
  or `photo` for anything unclassified.
- Photos arriving via Telegram default to `photo`; reclassify during review if
  it matters.
- Never rename a file after it has been referenced in a listing.

## Shot checklist

Three photos is the working minimum; six to eight is where identification
accuracy stops improving much.

- [ ] **Hero** — the whole item, straight on, best light, uncluttered background
- [ ] **Front** — square to the item, full frame
- [ ] **Back** — including any panel, ports, or unfinished surfaces
- [ ] **Left side**
- [ ] **Right side**
- [ ] **Top** — where the top surface is a selling point (tables, desks, cases)
- [ ] **Bottom / underside** — where the maker's mark or construction lives
- [ ] **Brand or model label** — close, in focus, readable
- [ ] **Serial or identifying number** — *only if it is safe to publish*; for
      electronics and tools, photograph it for your records but crop it out of
      the listing
- [ ] **Accessories** — everything that is included, laid out together
- [ ] **Dimensions** — a tape measure held against the longest edge
- [ ] **Defects** — one photo per flaw, close enough to be honest about it
- [ ] **Scale reference** — a common object (a mug, a can, a hand) for anything
      whose size is not obvious

### Practical notes

- Daylight, indirect. Overhead room light makes wood look grey and fabric look
  dirty.
- Clear the background. A cluttered floor reads as a cluttered house and
  lowers what people will pay.
- Shoot from the height the object is normally seen at, not from above.
- Wipe it first. Ten seconds with a cloth is worth more than any editing.
- Photograph defects deliberately. A buyer who finds an undisclosed scratch
  cancels the pickup; a buyer who saw it in photo 7 shows up anyway.
- Do not photograph anything you would not want a stranger to see: documents,
  screens with content, keys, family photos, or the view out of a window that
  identifies the street.

## What the intake flow asks for

The Telegram bot asks for 3–8 photos and suggests the specific extra shots the
vision model says would help — usually a label, a serial, or a defect close-up.
Those suggestions appear in the review interface too.

## Reclassifying slots

Slots are cosmetic today: they drive filenames and hero selection, not pricing.
The first photo received becomes the hero. To change the hero, edit the
`is_hero` flag on the `estate_photos` row and rebuild the site.
