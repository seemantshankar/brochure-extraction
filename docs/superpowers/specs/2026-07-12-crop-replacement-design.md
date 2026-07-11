# Crop replacement during HTML extraction

## Goal

When a user changes a committed crop and recommits it, the crop must replace its
previous extraction segment. Newly drawn crops must remain additions. A page
must therefore be assembled only from its currently committed crops.

## Design

The annotation client already sends a committed crop's filename when its box is
edited. The commit endpoint will treat that filename as the crop's stable
identity:

- Validate that the supplied filename belongs to the selected page.
- Recreate the image at the changed bounding box using the same filename.
- Replace that crop's bounding-box metadata in place instead of appending a new
  crop record.
- Continue creating a new `crop_NNN.png` only for a crop without a supplied
  filename.

After either an update or addition, the existing crop-mutation reconciliation
removes tasks and fragments for crops no longer present, retains the updated
crop's task identity, and removes the generated-output completion marker. The
updated crop's existing fragment will be invalidated before reconciliation so it
is extracted again. A new crop gets a pending task as it does today.

## Error handling

An update filename that is not a crop on the selected page is rejected rather
than accepted as an arbitrary file reference. Existing bounds and session
checks remain unchanged.

## Verification

A regression test will create and extract an initial large crop, then recommit
that crop at a smaller bounding box while adding another crop. It will assert
that only the two current crop tasks/fragments are included, and that the old
large-crop fragment is not assembled.
