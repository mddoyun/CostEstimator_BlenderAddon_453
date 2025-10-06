import sys
import os
import traceback

vendor_dir = os.path.join(os.path.dirname(__file__), 'lib')
if vendor_dir not in sys.path:
    sys.path.append(vendor_dir)
import webbrowser

import bpy
import json
import ifcopenshell
import ifcopenshell.api
import asyncio
import threading
import websockets
from bpy.app.handlers import persistent
import io  #  <-- 이 줄을 추가해주세요.

bl_info = {
    "name": "Cost Estimator Connector", "author": "AI Assistant & User",
    "description": "Cost Estimator 웹 애플리케이션과 실시간으로 통신합니다.",
    "blender": (4, 2, 0), "version": (1, 0, 5), # 버전 업데이트
    "location": "3D 뷰 > 사이드바(N) > Cost Estimator", "category": "Object",
}

websocket_client = None
event_queue = asyncio.Queue()
status_message = "연결 대기 중..."
websocket_thread_loop = None

def schedule_blender_task(task_callable, *args, **kwargs):
    def safe_task():
        try: task_callable(*args, **kwargs)
        except Exception as e: print(f"Blender 작업 실행 오류: {e}")
        return None
    bpy.app.timers.register(safe_task)

# ▼▼▼ [최종 수정 1] get_ifc_file 함수를 아래의 가장 안정적인 코드로 교체해주세요. ▼▼▼
def get_ifc_file():
    try:
        # BlenderBIM 프로젝트에 설정된 IFC 파일 경로를 가져옵니다.
        ifc_file_path = bpy.data.scenes["Scene"].BIMProperties.ifc_file
        if not ifc_file_path or not os.path.exists(ifc_file_path):
            return None, "IFC 파일 경로를 찾을 수 없습니다. BlenderBIM 프로젝트를 확인하세요."
        
        # 디스크에 저장된 파일을 직접 다시 엽니다. 이것이 가장 확실하고 안정적인 방법입니다.
        return ifcopenshell.open(ifc_file_path), None
    
    except Exception as e:
        print(f"IFC 파일을 여는 데 실패했습니다: {e}")
        return None, f"IFC 파일을 여는 데 실패했습니다: {e}"


def get_quantity_value(quantity):
    """IfcQuantity* 객체에서 실제 값(AreaValue, VolumeValue 등)을 추출하는 헬퍼 함수"""
    if quantity.is_a("IfcQuantityArea"): return quantity.AreaValue
    if quantity.is_a("IfcQuantityLength"): return quantity.LengthValue
    if quantity.is_a("IfcQuantityVolume"): return quantity.VolumeValue
    if quantity.is_a("IfcQuantityCount"): return quantity.CountValue
    if quantity.is_a("IfcQuantityWeight"): return quantity.WeightValue
    return None

def serialize_ifc_elements_to_string_list(ifc_file):
    """
    IFC 파일의 모든 IfcProduct(공간 구조 포함 모든 객체)와 관련된 데이터를 안정적으로 추출하여 직렬화합니다.
    """
    elements_data = []
    # [핵심 수정 1] 데이터 추출 대상을 IfcElement에서 다시 IfcProduct(모든 객체)로 변경합니다.
    products = ifc_file.by_type("IfcProduct")
    print(f"🔍 [Blender] ifc_file.by_type('IfcProduct')가 찾은 모든 객체 개수: {len(products)}")

    for element in products:
        if not element.GlobalId:
            continue

        element_dict = {
            "Name": element.Name or "이름 없음",
            "IfcClass": element.is_a(), # <--- 이 부분이 "IfcClass"인지 반드시 확인!
            "ElementId": element.id(),
            "UniqueId": element.GlobalId,
            "Parameters": {}, "TypeParameters": {}, "RelatingType": None,
            "SpatialContainer": None, "Aggregates": None, "Nests": None,
        }
        
        # [핵심 수정 2] 현재 객체가 공간 구조(Site, Building, Storey)인지 미리 확인합니다.
        is_spatial_element = element.is_a("IfcSpatialStructureElement")

        try:
            # 공통 속성 추출 (모든 객체 대상)
            if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                for definition in element.IsDefinedBy:
                    if definition.is_a("IfcRelDefinesByProperties"):
                        prop_set = definition.RelatingPropertyDefinition
                        if prop_set and prop_set.is_a("IfcPropertySet"):
                            pset_name = prop_set.Name
                            if hasattr(prop_set, 'HasProperties') and prop_set.HasProperties:
                                for prop in prop_set.HasProperties:
                                    if prop.is_a("IfcPropertySingleValue"):
                                        element_dict["Parameters"][f"{pset_name}.{prop.Name}"] = prop.NominalValue.wrappedValue if prop.NominalValue else None

            # [핵심 수정 3] 물리적 부재(공간 구조가 아닌 객체)일 경우에만 상세 속성을 추가로 추출합니다.
            if not is_spatial_element:
                if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                    for definition in element.IsDefinedBy:
                        if definition.is_a("IfcRelDefinesByProperties"):
                            prop_set = definition.RelatingPropertyDefinition
                            if prop_set and prop_set.is_a("IfcElementQuantity"):
                                qto_name = prop_set.Name
                                if hasattr(prop_set, 'Quantities') and prop_set.Quantities:
                                    for quantity in prop_set.Quantities:
                                        prop_value = get_quantity_value(quantity)
                                        if prop_value is not None:
                                            element_dict["Parameters"][f"{qto_name}.{quantity.Name}"] = prop_value

                if hasattr(element, 'IsTypedBy') and element.IsTypedBy:
                    type_definition = element.IsTypedBy[0]
                    if type_definition and type_definition.is_a("IfcRelDefinesByType"):
                        relating_type = type_definition.RelatingType
                        if relating_type:
                            element_dict["RelatingType"] = relating_type.Name
                            if hasattr(relating_type, 'HasPropertySets') and relating_type.HasPropertySets:
                                for prop_set in relating_type.HasPropertySets:
                                    if prop_set and prop_set.is_a("IfcPropertySet"):
                                        pset_name = prop_set.Name
                                        if hasattr(prop_set, 'HasProperties') and prop_set.HasProperties:
                                            for prop in prop_set.HasProperties:
                                                if prop.is_a("IfcPropertySingleValue"):
                                                    element_dict["TypeParameters"][f"{pset_name}.{prop.Name}"] = prop.NominalValue.wrappedValue if prop.NominalValue else None

                if hasattr(element, 'ContainedInStructure') and element.ContainedInStructure:
                    container = element.ContainedInStructure[0].RelatingStructure
                    element_dict["SpatialContainer"] = f"{container.is_a()}: {container.Name}"
            
            # 집합 관계는 모든 객체 유형에 대해 공통적으로 확인
            if hasattr(element, 'Decomposes') and element.Decomposes:
                aggregate = element.Decomposes[0].RelatingObject
                element_dict["Aggregates"] = f"{aggregate.is_a()}: {aggregate.Name}"

            if hasattr(element, 'Nests') and element.Nests:
                nest_parent = element.Nests[0].RelatingObject
                element_dict["Nests"] = f"{nest_parent.is_a()}: {nest_parent.Name}"

        except (AttributeError, IndexError, TypeError) as e:
            print(f"객체(ID: {element.id()}) 데이터 추출 중 경고: {e}")
            pass

        elements_data.append(json.dumps(element_dict))

    return elements_data

def get_selected_element_guids():
    """ 현재 선택된 객체들의 GlobalId 목록을 반환합니다. (참고 코드 적용) """
    guids = []
    ifc_file, error = get_ifc_file()
    if error:
        print(error)
        return guids

    for obj in bpy.context.selected_objects:
        # 1. Blender 객체에서 BlenderBIM의 내부 ID (STEP ID)를 가져옵니다.
        if hasattr(obj, "BIMObjectProperties") and hasattr(obj.BIMObjectProperties, "ifc_definition_id"):
            step_id = obj.BIMObjectProperties.ifc_definition_id
            if step_id:
                # 2. ifcopenshell을 사용해 STEP ID로 IFC 요소를 찾습니다.
                element = ifc_file.by_id(step_id)
                # 3. 찾은 IFC 요소에서 GlobalId를 추출합니다.
                if element and element.GlobalId:
                    guids.append(element.GlobalId)
    return guids

# ▼▼▼ [핵심 수정 2] '선택 확인'을 위한 함수 ▼▼▼
def select_elements_by_guids(guids):
    """ GlobalId 목록을 받아 해당하는 객체들을 선택합니다. (참고 코드 적용) """
    if not guids:
        bpy.ops.object.select_all(action='DESELECT')
        return

    ifc_file, error = get_ifc_file()
    if error:
        print(error)
        return
        
    # 1. 서버에서 받은 GlobalId 목록을 STEP ID 목록으로 변환합니다.
    target_step_ids = set()
    for guid in guids:
        element = ifc_file.by_guid(guid)
        if element:
            target_step_ids.add(element.id())

    if not target_step_ids:
        print("전달받은 GlobalId에 해당하는 IFC 객체를 찾을 수 없습니다.")
        return

    bpy.ops.object.select_all(action='DESELECT')
    target_objects = []
    
    # 2. Blender의 모든 객체를 순회하며, STEP ID가 일치하는 객체를 찾습니다.
    for obj in bpy.context.scene.objects:
        if hasattr(obj, "BIMObjectProperties") and hasattr(obj.BIMObjectProperties, "ifc_definition_id"):
            if obj.BIMObjectProperties.ifc_definition_id in target_step_ids:
                obj.select_set(True)
                target_objects.append(obj)

    if target_objects:
        bpy.context.view_layer.objects.active = target_objects[0]
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                override = {'area': area, 'region': next(r for r in area.regions if r.type == 'WINDOW')}
                with bpy.context.temp_override(**override):
                    bpy.ops.view3d.view_selected(use_all_regions=False)
                break

# --- WebSocket 통신 로직 ---
def send_message_to_server(message_dict):
    if websocket_client and websocket_thread_loop:
        asyncio.run_coroutine_threadsafe(
            websocket_client.send(json.dumps(message_dict)), websocket_thread_loop
        )

async def websocket_handler(uri):
    global websocket_client, status_message
    try:
        async with websockets.connect(uri) as websocket:
            websocket_client = websocket; status_message = "서버에 연결되었습니다."
            while True:
                try:
                    message_str = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    print(f"✉️  [Blender] 서버로부터 메시지 수신: {message_str}")
                    message_data = json.loads(message_str)
                    await event_queue.put(message_data)
                except asyncio.TimeoutError: continue
                except websockets.exceptions.ConnectionClosed: break
    except Exception as e: status_message = f"연결 실패: {e}"; traceback.print_exc()
    finally: status_message = "연결이 끊어졌습니다."; websocket_client = None

def run_websocket_in_thread(uri):
    def loop_in_thread():
        global websocket_thread_loop
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        websocket_thread_loop = loop
        loop.run_until_complete(websocket_handler(uri))
        loop.close()
    thread = threading.Thread(target=loop_in_thread, daemon=True); thread.start()

def process_event_queue_timer():
    try:
        while not event_queue.empty():
            command_data = event_queue.get_nowait()
            command = command_data.get("command")
            print(f"⚡️ [Blender] 이벤트 큐에서 명령 처리 시작: {command}")
            if command == "fetch_all_elements_chunked":
                schedule_blender_task(handle_fetch_all_elements, command_data)
            elif command == "get_selection":
                schedule_blender_task(handle_get_selection)
            elif command == "select_elements":
                guids = command_data.get("unique_ids", [])
                schedule_blender_task(select_elements_by_guids, guids)
    except Exception as e: print(f"이벤트 큐 처리 중 오류: {e}")
    return 0.1

# --- 서버 명령 처리 함수 ---
def handle_fetch_all_elements(command_data):
    print("🚀 [Blender] handle_fetch_all_elements 함수 실행됨.")
    global status_message
    if not websocket_client: return
    project_id = command_data.get("project_id")
    status_message = "IFC 데이터 추출 중..."; ifc_file, error = get_ifc_file()
    if error: status_message = error; return
    elements_data = serialize_ifc_elements_to_string_list(ifc_file)
    total_elements = len(elements_data)
    send_message_to_server({"type": "fetch_progress_start", "payload": {"total_elements": total_elements, "project_id": project_id}})
    status_message = f"{total_elements}개 객체 전송 중..."
    chunk_size = 100
    for i in range(0, total_elements, chunk_size):
        chunk = elements_data[i:i+chunk_size]
        processed_count = i + len(chunk)
        send_message_to_server({
            "type": "fetch_progress_update",
            "payload": {"project_id": project_id, "processed_count": processed_count, "elements": chunk}
        })
    send_message_to_server({"type": "fetch_progress_complete", "payload": {"total_sent": total_elements}})
    status_message = "데이터 전송 완료."

def handle_get_selection():
    selected_guids = get_selected_element_guids()
    print(f"✅ [Blender] {len(selected_guids)}개 객체 선택됨. 서버로 전송합니다.")
    send_message_to_server({"type": "revit_selection_response", "payload": selected_guids})
    global status_message; status_message = f"{len(selected_guids)}개 객체 선택 정보 전송."

class COSTESTIMATOR_OT_Connect(bpy.types.Operator):
    bl_idname = "costestimator.connect"; bl_label = "서버에 연결"
    def execute(self, context):
        global status_message
        if websocket_client:
            self.report({'WARNING'}, "이미 연결되어 있습니다.")
            return {'CANCELLED'}
        
        uri = context.scene.costestimator_server_url

        # --- 이 부분이 추가되었습니다 ---
        try:
            # ws://127.0.0.1:8000/ws/blender-connector/ -> http://127.0.0.1:8000
            base_address = uri.replace("ws://", "").replace("wss://", "").split("/")[0]
            web_url = f"http://{base_address}"
            webbrowser.open(web_url)
            print(f"웹 브라우저에서 {web_url} 주소를 엽니다.")
        except Exception as e:
            print(f"웹 브라우저를 여는 데 실패했습니다: {e}")
            self.report({'WARNING'}, f"웹 브라우저 열기 실패: {e}")
        # --- 여기까지 추가 ---

        status_message = "서버에 연결 시도 중..."
        run_websocket_in_thread(uri)
        return {'FINISHED'}

class COSTESTIMATOR_OT_Disconnect(bpy.types.Operator):
    bl_idname = "costestimator.disconnect"; bl_label = "연결 끊기"
    def execute(self, context):
        global websocket_client, status_message, websocket_thread_loop
        if not websocket_client: self.report({'WARNING'}, "연결되어 있지 않습니다."); return {'CANCELLED'}
        if websocket_thread_loop: asyncio.run_coroutine_threadsafe(websocket_client.close(), websocket_thread_loop)
        websocket_client = None; websocket_thread_loop = None; status_message = "연결이 끊어졌습니다."
        return {'FINISHED'}

class COSTESTIMATOR_PT_Panel(bpy.types.Panel):
    bl_label = "Cost Estimator"; bl_idname = "COSTESTIMATOR_PT_Panel"
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = 'Cost Estimator'
    def draw(self, context):
        layout = self.layout; scene = context.scene
        layout.prop(scene, "costestimator_server_url")
        split = layout.split(factor=0.5, align=True)
        col1 = split.column(); col1.active = websocket_client is None
        col1.operator("costestimator.connect", text="연결", icon='PLAY')
        col2 = split.column(); col2.active = websocket_client is not None
        col2.operator("costestimator.disconnect", text="연결 끊기", icon='PAUSE')
        layout.label(text=f"상태: {status_message}")

classes = (COSTESTIMATOR_OT_Connect, COSTESTIMATOR_OT_Disconnect, COSTESTIMATOR_PT_Panel)

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.costestimator_server_url = bpy.props.StringProperty(
        name="서버 주소", default="ws://127.0.0.1:8000/ws/blender-connector/"
    )
    bpy.app.timers.register(process_event_queue_timer)

def unregister():
    if bpy.app.timers.is_registered(process_event_queue_timer):
        bpy.app.timers.unregister(process_event_queue_timer)
    if websocket_client and websocket_thread_loop:
        asyncio.run_coroutine_threadsafe(websocket_client.close(), websocket_thread_loop)
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.costestimator_server_url

if __name__ == "__main__":
    register()