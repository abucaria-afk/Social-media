bl_info = {
    "name": "Auteur Blender: Final Check Agent",
    "author": "AI Collaborator & Contributor",
    "version": (1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Auteur Tab",
    "description": "Internal automation for checking vertical aspect ratios, applying overlays, and syncing SFX.",
    "category": "Interface",
}

import bpy
import os
import json

class AUTEUR_OT_run_final_check(bpy.types.Operator):
    """Executes safe-zone checks, composites visual stickers, and inserts frame-accurate sound FX."""
    bl_idname = "auteur.run_final_check"
    bl_label = "Run Final Check Pipeline"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        
        # --- 1. RESOLUTION VALIDATION & AUTO-CORRECTION ---
        target_x = 1080
        target_y = 1920
        
        if scene.render.resolution_x != target_x or scene.render.resolution_y != target_y:
            self.report({'WARNING'}, f"Resolution corrected from {scene.render.resolution_x}x{scene.render.resolution_y} to {target_x}x{target_y}")
            scene.render.resolution_x = target_x
            scene.render.resolution_y = target_y
        else:
            self.report({'INFO'}, "Dimensions Verified: Target vertical format matching 1080x1920 perfectly.")

        # --- 2. MANIFEST PARSING (OPTIONAL) ---
        manifest_path = os.path.join(scene.auteur_asset_dir, "edit_manifest.json")
        target_frame = scene.auteur_sfx_frame
        
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r') as f:
                    data = json.load(f)
                    if "audio_cues" in data and len(data["audio_cues"]) > 0:
                        target_frame = data["audio_cues"][0].get("frame_trigger", target_frame)
                        self.report({'INFO'}, f"Overrode sync frame from manifest target: {target_frame}")
            except Exception as e:
                self.report({'WARNING'}, f"Could not parse manifest metadata: {str(e)}")

        # --- 3. VISUAL LAYER: AUTOMATED COMPOSITOR NODES ---
        scene.use_nodes = True
        node_tree = scene.node_tree
        nodes = node_tree.nodes
        nodes.clear()  

        render_layers = nodes.new(type='CompositorNodeRLayers')
        render_layers.location = (-300, 200)
        
        composite_out = nodes.new(type='CompositorNodeComposite')
        composite_out.location = (300, 200)

        overlay_node = nodes.new(type='CompositorNodeAlphaOver')
        overlay_node.location = (0, 200)
        
        image_node = nodes.new(type='CompositorNodeImage')
        image_node.location = (-300, -100)

        sticker_path = os.path.join(scene.auteur_asset_dir, "branding_sticker.png")
        if os.path.exists(sticker_path):
            try:
                loaded_img = bpy.data.images.load(sticker_path)
                image_node.image = loaded_img
            except Exception as e:
                self.report({'ERROR'}, f"Failed to load branding sticker: {str(e)}")
        else:
            self.report({'WARNING'}, "No branding_sticker.png found. Created placeholder nodes.")

        node_tree.links.new(render_layers.outputs['Image'], overlay_node.inputs[1])
        node_tree.links.new(image_node.outputs['Image'], overlay_node.inputs[2])
        node_tree.links.new(overlay_node.outputs['Image'], composite_out.inputs['Image'])

        # --- 4. AUDIO LAYER: VIDEO SEQUENCE EDITOR (VSE) SYNCHRONIZATION ---
        if not scene.sequence_editor:
            scene.sequence_editor_create()
            
        vse = scene.sequence_editor
        sfx_path = os.path.join(scene.auteur_asset_dir, "hit_marker_effect.wav")
        audio_channel = 3

        if os.path.exists(sfx_path):
            duplicates = [s for s in vse.sequences if s.frame_start == target_frame and s.channel == audio_channel]
            if not duplicates:
                vse.sequences.new_sound(
                    name="Auteur_Check_SFX",
                    filepath=sfx_path,
                    channel=audio_channel,
                    frame_start=target_frame
                )
                self.report({'INFO'}, f"SFX successfully synchronized at frame {target_frame}.")
            else:
                self.report({'INFO'}, "SFX sequence strip already exists at timestamp, skipped duplication.")
        else:
            self.report({'WARNING'}, "Audio asset file missing. VSE track allocation skipped.")

        # --- 5. AUTOMATED RENDER EXPORT LOOP ---
        if scene.auteur_auto_render:
            self.report({'INFO'}, "Validation passed. Initializing background MP4 render loop...")
            scene.render.image_settings.file_format = 'FFMPEG'
            scene.render.ffmpeg.format = 'MPEG4'
            scene.render.ffmpeg.codec = 'H264'
            scene.render.ffmpeg.audio_codec = 'AAC'
            scene.render.filepath = os.path.join(scene.auteur_asset_dir, "exports", "auteur_final_output.mp4")
            bpy.ops.render.render(animation=True, write_still=True)
            self.report({'INFO'}, "Render Loop Completed Successfully.")

        return {'FINISHED'}


class AUTEUR_PT_agent_panel(bpy.types.Panel):
    """Creates a dedicated UI Sidebar panel inside the 3D Viewport."""
    bl_label = "Auteur Final Check UI"
    bl_idname = "AUTEUR_PT_agent_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Auteur"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Agent File Environments", icon='FOLDER_REDIRECT')
        box.prop(scene, "auteur_asset_dir", text="Asset Path")
        
        box = layout.box()
        box.label(text="Timeline Sync Configurations", icon='TIME')
        box.prop(scene, "auteur_sfx_frame", text="SFX Marker Frame")
        box.prop(scene, "auteur_auto_render", text="Auto Render on Pass")

        layout.separator()
        layout.operator("auteur.run_final_check", icon='CHECKMARK', text="Run Final Check Pipeline")


def register():
    bpy.types.Scene.auteur_asset_dir = bpy.props.StringProperty(
        name="Asset Directory",
        subtype='DIR_PATH',
        default=""
    )
    bpy.types.Scene.auteur_sfx_frame = bpy.props.IntProperty(
        name="SFX Playback Frame",
        default=24,
        min=1
    )
    bpy.types.Scene.auteur_auto_render = bpy.props.BoolProperty(
        name="Auto Render",
        default=False
    )
    bpy.utils.register_class(AUTEUR_OT_run_final_check)
    bpy.utils.register_class(AUTEUR_PT_agent_panel)

def unregister():
    bpy.utils.unregister_class(AUTEUR_PT_agent_panel)
    bpy.utils.unregister_class(AUTEUR_OT_run_final_check)
    del bpy.types.Scene.auteur_asset_dir
    del bpy.types.Scene.auteur_sfx_frame
    del bpy.types.Scene.auteur_auto_render

if __name__ == "__main__":
    register()
