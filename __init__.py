import bpy
import ifcopenshell
import ifcopenshell.api

# 애드온 정보
bl_info = {
    "name": "BIM 자동 검색 애드온 (최종판)",
    "author": "AI Assistant & User",
    "description": "현재 로드된 IFC 파일에서 GlobalId로 객체를 자동으로 검색하고 선택합니다.",
    "blender": (4, 2, 0),
    "version": (17, 0, 1), # 버전 마이너 업데이트
    "location": "3D 뷰 > 사이드바(N) > BIM 자동 검색",
    "category": "Object",
}


# ----------------------------------
# 1. 실제 검색 및 선택을 수행할 Operator 정의
# ----------------------------------
class FINAL_OT_AutoSearch(bpy.types.Operator):
    """현재 로드된 IFC 파일을 자동으로 찾아 객체를 선택하는 최종 Operator"""
    bl_idname = "final.auto_search"
    bl_label = "GlobalId로 자동 검색 및 선택"
    bl_options = {'REGISTER', 'UNDO'}

    global_id_to_find: bpy.props.StringProperty()

    def execute(self, context):
        # --- 1단계: 사용자님이 찾아주신 정확한 주소로 IFC 파일 경로 가져오기 ---
        try:
            ifc_file_path = bpy.data.scenes["Scene"].BIMProperties.ifc_file
            if not ifc_file_path:
                self.report({'ERROR'}, "Bonsai 프로젝트의 IFC 파일 경로를 찾을 수 없습니다. IFC 프로젝트가 올바르게 열려있는지 확인하세요.")
                return {'CANCELLED'}
        except (AttributeError, KeyError):
            self.report({'ERROR'}, "Bonsai 프로젝트 속성(BIMProperties)을 찾을 수 없습니다.")
            return {'CANCELLED'}

        # --- 2단계: 가져온 경로로 IFC 파일을 직접 열기 ---
        try:
            ifc_file = ifcopenshell.open(ifc_file_path)
        except Exception as e:
            self.report({'ERROR'}, f"'{ifc_file_path}' 파일을 여는 데 실패했습니다: {e}")
            return {'CANCELLED'}

        # --- 3단계: IFC 세계에서 객체 찾기 ---
        element = ifc_file.by_guid(self.global_id_to_find)
        if not element:
            self.report({'WARNING'}, f"IFC 파일에서 '{self.global_id_to_find}' GlobalId를 찾지 못했습니다.")
            return {'CANCELLED'}
        
        target_step_id = element.id()
        
        # --- 4단계: Blender 세계에서 객체 찾기 ---
        target_object = None
        for obj in context.scene.objects:
            if hasattr(obj, "BIMObjectProperties") and hasattr(obj.BIMObjectProperties, "ifc_definition_id"):
                if obj.BIMObjectProperties.ifc_definition_id == target_step_id:
                    target_object = obj
                    break

        if not target_object:
            self.report({'WARNING'}, "IFC 요소는 찾았지만, 현재 씬에 해당하는 Blender 객체가 없습니다.")
            return {'CANCELLED'}

        # --- 5단계: 찾은 객체 선택 및 컨텍스트 문제 해결 ---
        bpy.ops.object.select_all(action='DESELECT')
        target_object.select_set(True)
        context.view_layer.objects.active = target_object
        
        # (핵심 수정!) 3D 뷰포트 컨텍스트를 완벽하게 구성하여 전달
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                # 3D 뷰 영역(area) 내에서 실제 뷰가 표시되는 'WINDOW' 타입의 세부 구역(region)을 찾습니다.
                for region in area.regions:
                    if region.type == 'WINDOW':
                        # 실행에 필요한 모든 컨텍스트 정보를 담은 사전을 만듭니다.
                        override_context = {
                            'window': context.window,
                            'screen': context.screen,
                            'area': area,
                            'region': region,
                            'scene': context.scene
                        }
                        # 완성된 컨텍스트로 view_selected를 실행합니다.
                        with context.temp_override(**override_context):
                            bpy.ops.view3d.view_selected()
                        break # 올바른 region을 찾았으므로 더 이상 순회할 필요가 없습니다.
                break # 올바른 area를 찾았으므로 더 이상 순회할 필요가 없습니다.

        self.report({'INFO'}, f"객체 '{target_object.name}'를 선택했습니다.")
        return {'FINISHED'}

# (이하 UI 및 등록 코드는 이전과 동일)
class FINAL_PT_AutoSearchPanel(bpy.types.Panel):
    bl_label = "BIM 자동 검색"
    bl_idname = "FINAL_PT_auto_search_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BIM 자동 검색'

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "final_addon_global_id_input")
        op = layout.operator("final.auto_search", text="검색 및 선택")
        op.global_id_to_find = context.scene.final_addon_global_id_input

classes = (FINAL_OT_AutoSearch, FINAL_PT_AutoSearchPanel)

def register():
    bpy.types.Scene.final_addon_global_id_input = bpy.props.StringProperty(name="GlobalId", description="검색할 객체의 GlobalId")
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.final_addon_global_id_input

if __name__ == "__main__":
    register()