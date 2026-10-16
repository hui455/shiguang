---
name: ui-image-gen
description: Use when a design, page or app needs raster art — illustrations, hero images, character portraits, card art, icons, backgrounds, empty-state art, or a full-screen visual style reference — and the openai-image-gen tools are available.
---

# UI image generation

Use the `openai-image-gen` MCP tools to produce image assets, then place them into the design or code.

## Workflow

1. **Work out what the layout needs first.** List each image slot (hero, avatar, card art, background, icon) with its aspect ratio and role. Generate only what the layout actually uses.
2. **Lock a shared style.** Write one style line and reuse it in every prompt so assets match, e.g.
   `Style: Chinese ink-wash with gold leaf accents, muted crimson and jade palette, soft rim light.`
3. **Pick the size from the slot:**
   - square card / avatar / icon → `1024x1024`
   - hero / banner / landscape background → `1536x1024`
   - portrait card / phone background / character standing art → `1024x1536`
4. **Transparent assets** (icons, stickers, character cut-outs, sprites): `background="transparent"`, `output_format="png"`.
5. **Name files by role** with `filename` (e.g. `hero-home`, `card-guanyu`) so code references stay readable.
6. **Consistency across a set:** generate the first asset, then use `edit_image` with it as a reference for the rest ("same character, new pose", "same style, different hero").
7. **Draft cheap, finish high.** Explore with `quality="low"`, regenerate the chosen one with `quality="high"`.
8. **Reference the saved path** from the tool result in the HTML/code. Never embed images as base64 in source unless asked.

## Style reference for a whole screen

To set a visual direction before building UI, generate a full mock screen as an image
(`1024x1536` for mobile), look at it, then rebuild it as real, editable UI. The image is a reference, not the deliverable.

## Prompt shape

`[subject + action], [composition / framing], [style line], [lighting], [palette], [what to avoid]`

Avoid asking for text inside images unless the model handles it well; real UI text belongs in the layout.

## Reporting results

The preview returned by the tools is a low-res thumbnail for your own review; the user
should look at the full-resolution file. When download links are returned (remote mode),
end the reply with a `ps:` block: one line per image, the full-resolution download URL as a
plain Markdown image, so the original shows sharp in chat. Never wrap it in a link
(`[![..](..)](..)` shows up as broken text in the Claude app) and add nothing else. Include
only images generated for this reply, never earlier ones or `list_images` results:

```
ps:
![card-guanyu.png](https://…/files/…/card-guanyu.png)
![card-zhangfei.png](https://…/files/…/card-zhangfei.png)
```

## Tips

- Set `preview=false` when generating many images to save context; preview only the ones you need to judge.
- `image_config` shows which model and endpoint are active.
- Every image keeps its prompt, parameters and references. To redo or vary an earlier image,
  call `image_info` on it and start from the saved prompt instead of rewriting it from memory.
- Respect copyright: don't request recognizable trademarked characters, logos or living artists' signature styles.
