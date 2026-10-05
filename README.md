# ComfyUI-Custom-Node-Installer

Store all your custom-node git links in one node, install them in one click, and auto-find
the repos for any nodes a loaded workflow is missing.

Install: copy this folder to `ComfyUI/custom_nodes/`, restart ComfyUI (needs `git` on the machine).
Add node: right-click -> Add Node -> utils -> model downloader -> *Custom Node Installer*.

Missing-node lookup order: workflow metadata (`aux_id` / `cnr_id`) -> Comfy Registry -> ComfyUI-Manager's public
node database (downloaded & cached for 12h in `cache/`).
Repos named only by workflow metadata are unverified and are never auto-installed.
