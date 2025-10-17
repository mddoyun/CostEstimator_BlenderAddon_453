#
# 블렌더 애드온 최종 수정 코드
#
import sys
import os
import traceback
import platform #<-- 운영체제 감지를 위해 추가

# --- ▼▼▼ [핵심 수정] 라이브러리 경로 추가 코드를 맨 위로 이동 ▼▼▼ ---
# 1. 애드온의 루트 디렉토리(__file__가 있는 곳)를 기준으로 'lib' 폴더의 절대 경로를 생성합니다.
lib_dir = os.path.join(os.path.dirname(__file__), 'lib')

# 2. 생성된 경로를 파이썬 모듈 검색 경로 리스트(sys.path)에 추가합니다.
#    이렇게 하면 'lib' 폴더 안에 있는 'websockets' 같은 라이브러리를 import 할 수 있게 됩니다.
if lib_dir not in sys.path:
    sys.path.append(lib_dir)
# --- ▲▲▲ 여기까지가 핵심 수정입니다 ▲▲▲ ---

# --- 이제 외부 라이브러리를 import 합니다. ---
import bpy
import json
import ifcopenshell
import ifcopenshell.api
import asyncio
import threading
import websockets # <- 이제 이 import가 정상적으로 동작합니다.
from bpy.app.handlers import persistent
import io
import subprocess
import time
import urllib.request
import webbrowser


bl_info = {
    "name": "Cost Estimator Connector", "author": "AI Assistant & User",
    "description": "Cost Estimator 웹 애플리케이션과 실시간으로 통신합니다.",
    "blender": (4, 2, 0), "version": (1, 2, 0), # 버전 업데이트
    "location": "3D 뷰 > 사이드바(N) > Cost Estimator", "category": "Object",
}

# --- 전역 변수 관리 ---
websocket_client = None
event_queue = asyncio.Queue()
status_message = "연결 대기 중..."
websocket_thread_loop = None

server_process = None
server_status = "서버 꺼짐" # "서버 꺼짐", "시작 중...", "실행 중", "오류"
SERVER_CHECK_TIMEOUT = 30 


def schedule_blender_task(task_callable, *args, **kwargs):
    def safe_task():
        try: task_callable(*args, **kwargs)
        except Exception as e: print(f"Blender 작업 실행 오류: {e}")
        return None
    bpy.app.timers.register(safe_task)


def stop_server_process():
    """백그라운드에서 실행 중인 Django 서버 프로세스를 종료합니다."""
    global server_process, server_status
    if server_process and server_process.poll() is None:
        print("🔌 [Blender] Django 서버 프로세스를 종료합니다...")
        try:
            server_process.terminate() 
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill() 
            print("🛑 [Blender] 서버 프로세스가 응답하지 않아 강제 종료했습니다.")
        except Exception as e:
            print(f"서버 프로세스 종료 중 오류: {e}")
        
    server_process = None
    server_status = "서버 꺼짐"
    print("✅ [Blender] 서버가 성공적으로 종료되었습니다.")


def get_ifc_file():
    try:
        ifc_file_path = bpy.data.scenes["Scene"].BIMProperties.ifc_file
        if not ifc_file_path or not os.path.exists(ifc_file_path):
            return None, "IFC 파일 경로를 찾을 수 없습니다. BlenderBIM 프로젝트를 확인하세요."
        return ifcopenshell.open(ifc_file_path), None
    except Exception as e:
        print(f"IFC 파일을 여는 데 실패했습니다: {e}")
        return None, f"IFC 파일을 여는 데 실패했습니다: {e}"

def get_quantity_value(quantity):
    if quantity.is_a("IfcQuantityArea"): return quantity.AreaValue
    if quantity.is_a("IfcQuantityLength"): return quantity.LengthValue
    if quantity.is_a("IfcQuantityVolume"): return quantity.VolumeValue
    if quantity.is_a("IfcQuantityCount"): return quantity.CountValue
    if quantity.is_a("IfcQuantityWeight"): return quantity.WeightValue
    return None

def serialize_ifc_elements_to_string_list(ifc_file):
    elements_data = []
    products = ifc_file.by_type("IfcProduct")
    print(f"🔍 [Blender] {len(products)}개의 IFC 객체 데이터 직렬화를 시작합니다.") # 디버깅 추가
    for element in products:
        if not element.GlobalId: continue
        element_dict = { "Name": element.Name or "이름 없음", "IfcClass": element.is_a(), "ElementId": element.id(), "UniqueId": element.GlobalId, "Parameters": {}, "TypeParameters": {}, "RelatingType": None, "SpatialContainer": None, "Aggregates": None, "Nests": None, }
        is_spatial_element = element.is_a("IfcSpatialStructureElement")
        try:
            if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                for definition in element.IsDefinedBy:
                    if definition.is_a("IfcRelDefinesByProperties"):
                        prop_set = definition.RelatingPropertyDefinition
                        if prop_set and prop_set.is_a("IfcPropertySet"):
                            if hasattr(prop_set, 'HasProperties') and prop_set.HasProperties:
                                for prop in prop_set.HasProperties:
                                    # ▼▼▼ [수정] 구분자를 '.'에서 '__'로 변경 ▼▼▼
                                    if prop.is_a("IfcPropertySingleValue"): 
                                        prop_key = f"{prop_set.Name}__{prop.Name}"
                                        element_dict["Parameters"][prop_key] = prop.NominalValue.wrappedValue if prop.NominalValue else None
                                        # print(f"  - 파라미터 추가: {prop_key}") # 상세 디버깅 필요시 주석 해제
            if not is_spatial_element:
                if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                    for definition in element.IsDefinedBy:
                        if definition.is_a("IfcRelDefinesByProperties"):
                            prop_set = definition.RelatingPropertyDefinition
                            if prop_set and prop_set.is_a("IfcElementQuantity"):
                                if hasattr(prop_set, 'Quantities') and prop_set.Quantities:
                                    for quantity in prop_set.Quantities:
                                        prop_value = get_quantity_value(quantity)
                                        if prop_value is not None:
                                            # ▼▼▼ [수정] 구분자를 '.'에서 '__'로 변경 ▼▼▼
                                            prop_key = f"{prop_set.Name}__{quantity.Name}"
                                            element_dict["Parameters"][prop_key] = prop_value
                                            # print(f"  - 수량 파라미터 추가: {prop_key}") # 상세 디버깅 필요시 주석 해제
                if hasattr(element, 'IsTypedBy') and element.IsTypedBy:
                    type_definition = element.IsTypedBy[0]
                    if type_definition and type_definition.is_a("IfcRelDefinesByType"):
                        relating_type = type_definition.RelatingType
                        if relating_type:
                            element_dict["RelatingType"] = relating_type.Name
                            if hasattr(relating_type, 'HasPropertySets') and relating_type.HasPropertySets:
                                for prop_set in relating_type.HasPropertySets:
                                    if prop_set and prop_set.is_a("IfcPropertySet"):
                                        if hasattr(prop_set, 'HasProperties') and prop_set.HasProperties:
                                            for prop in prop_set.HasProperties:
                                                # ▼▼▼ [수정] 구분자를 '.'에서 '__'로 변경 ▼▼▼
                                                if prop.is_a("IfcPropertySingleValue"): 
                                                    prop_key = f"{prop_set.Name}__{prop.Name}"
                                                    element_dict["TypeParameters"][prop_key] = prop.NominalValue.wrappedValue if prop.NominalValue else None
                                                    # print(f"  - 타입 파라미터 추가: {prop_key}") # 상세 디버깅 필요시 주석 해제
                if hasattr(element, 'ContainedInStructure') and element.ContainedInStructure: element_dict["SpatialContainer"] = f"{element.ContainedInStructure[0].RelatingStructure.is_a()}: {element.ContainedInStructure[0].RelatingStructure.Name}"
            if hasattr(element, 'Decomposes') and element.Decomposes: element_dict["Aggregates"] = f"{element.Decomposes[0].RelatingObject.is_a()}: {element.Decomposes[0].RelatingObject.Name}"
            if hasattr(element, 'Nests') and element.Nests: element_dict["Nests"] = f"{element.Nests[0].RelatingObject.is_a()}: {element.Nests[0].RelatingObject.Name}"
        except (AttributeError, IndexError, TypeError): pass
        elements_data.append(json.dumps(element_dict))
    print(f"✅ [Blender] 객체 데이터 직렬화 완료.") # 디버깅 추가
    return elements_data
def get_selected_element_guids():
    guids = []
    ifc_file, error = get_ifc_file()
    if error: return guids
    for obj in bpy.context.selected_objects:
        if hasattr(obj, "BIMObjectProperties") and hasattr(obj.BIMObjectProperties, "ifc_definition_id"):
            step_id = obj.BIMObjectProperties.ifc_definition_id
            if step_id:
                element = ifc_file.by_id(step_id)
                if element and element.GlobalId: guids.append(element.GlobalId)
    return guids

def select_elements_by_guids(guids):
    if not guids:
        bpy.ops.object.select_all(action='DESELECT')
        return
    ifc_file, error = get_ifc_file()
    if error: return
    target_step_ids = {ifc_file.by_guid(guid).id() for guid in guids if ifc_file.by_guid(guid)}
    if not target_step_ids: return
    bpy.ops.object.select_all(action='DESELECT')
    target_objects = []
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
                with bpy.context.temp_override(**override): bpy.ops.view3d.view_selected(use_all_regions=False)
                break

def send_message_to_server(message_dict):
    if websocket_client and websocket_thread_loop: asyncio.run_coroutine_threadsafe(websocket_client.send(json.dumps(message_dict)), websocket_thread_loop)

async def websocket_handler(uri):
    global websocket_client, status_message
    try:
        async with websockets.connect(uri) as websocket:
            websocket_client = websocket; status_message = "서버에 연결되었습니다."
            while True:
                try:
                    message_str = await asyncio.wait_for(websocket.recv(), timeout=1.0)
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
            if command == "fetch_all_elements_chunked": schedule_blender_task(handle_fetch_all_elements, command_data)
            elif command == "get_selection": schedule_blender_task(handle_get_selection)
            elif command == "select_elements": schedule_blender_task(select_elements_by_guids, command_data.get("unique_ids", []))
    except Exception as e: print(f"이벤트 큐 처리 중 오류: {e}")
    return 0.1

def handle_fetch_all_elements(command_data):
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
        send_message_to_server({"type": "fetch_progress_update", "payload": {"project_id": project_id, "processed_count": processed_count, "elements": chunk}})
    send_message_to_server({"type": "fetch_progress_complete", "payload": {"total_sent": total_elements}})
    status_message = "데이터 전송 완료."

def handle_get_selection():
    selected_guids = get_selected_element_guids()
    send_message_to_server({"type": "revit_selection_response", "payload": selected_guids})
    global status_message; status_message = f"{len(selected_guids)}개 객체 선택 정보 전송."


start_time = 0
def check_server_status():
    """0.5초마다 서버 상태를 확인하는 타이머 함수"""
    global server_status, start_time

    if time.time() - start_time > SERVER_CHECK_TIMEOUT:
        print("🛑 [Blender] 서버 시작 시간 초과.")
        server_status = "오류: 시간 초과"
        stop_server_process()
        return None 

    try:
        uri = bpy.context.scene.costestimator_server_url
        base_address = uri.replace("ws://", "http://").replace("wss://", "").split("/ws/")[0]
        with urllib.request.urlopen(base_address, timeout=1) as response:
            if response.status == 200:
                print("✅ [Blender] 서버가 성공적으로 실행되었습니다.")
                server_status = "실행 중"
                return None 
    except Exception:
        return 0.5 

# --- ▼▼▼ [핵심 수정] 서버 시작 Operator 수정 ▼▼▼ ---
class COSTESTIMATOR_OT_StartServer(bpy.types.Operator):
    bl_idname = "costestimator.start_server"
    bl_label = "로컬 서버 시작"
    bl_description = "Cost Estimator 웹 서버를 백그라운드에서 실행합니다."

    def execute(self, context):
        global server_process, server_status, start_time
        if server_process and server_process.poll() is None:
            self.report({'WARNING'}, "서버가 이미 실행 중입니다.")
            return {'CANCELLED'}

        addon_dir = os.path.dirname(__file__)
        executable_path = None

        # 1. 운영체제를 확인하고 그에 맞는 실행 파일 경로를 설정합니다.
        if platform.system() == "Windows":
            executable_path = os.path.join(addon_dir, "server_win", "CostEstimatorServer.exe")
        elif platform.system() == "Darwin": # "Darwin"은 macOS의 공식 명칭입니다.
            executable_path = os.path.join(addon_dir, "server_mac", "CostEstimatorServer")
        else:
            self.report({'ERROR'}, f"지원하지 않는 운영체제입니다: {platform.system()}")
            return {'CANCELLED'}
        
        # 2. 실행 파일이 실제로 존재하는지 확인합니다.
        if not os.path.exists(executable_path):
            msg = f"실행 파일을 찾을 수 없습니다: {executable_path}"
            self.report({'ERROR'}, msg)
            server_status = "오류: 파일 없음"
            return {'CANCELLED'}

        try:
            # 3. macOS인 경우, 실행 권한을 부여합니다. (최초 1회만 필요)
            if platform.system() == "Darwin":
                try:
                    # 'chmod +x'와 동일한 효과
                    os.chmod(executable_path, 0o755)
                    print(f"macOS 실행 권한을 설정했습니다: {executable_path}")
                except Exception as e:
                    print(f"경고: 실행 권한 설정에 실패했습니다. 이미 권한이 있을 수 있습니다. ({e})")

            print(f"🚀 [Blender] 서버 실행 시도: {executable_path}")
            
            # 4. 백그라운드에서 서버 프로세스 시작
            #    Windows에서는 터미널 창이 뜨지 않도록 CREATE_NO_WINDOW 플래그를 추가합니다.
            creation_flags = 0
            if platform.system() == "Windows":
                creation_flags = subprocess.CREATE_NO_WINDOW

            server_process = subprocess.Popen([executable_path], creationflags=creation_flags)
            server_status = "시작 중..."
            
            # 5. 서버 상태 확인 타이머 시작
            start_time = time.time()
            bpy.app.timers.register(check_server_status)
            
            self.report({'INFO'}, "서버를 시작합니다. 잠시만 기다려주세요...")
        except Exception as e:
            msg = f"서버 시작 실패: {e}"
            self.report({'ERROR'}, msg)
            server_status = "오류"
            server_process = None
            return {'CANCELLED'}

        return {'FINISHED'}
# --- ▲▲▲ [핵심 수정] 여기까지 입니다 ▲▲▲ ---


class COSTESTIMATOR_OT_Connect(bpy.types.Operator):
    bl_idname = "costestimator.connect"
    bl_label = "웹소켓 연결 및 브라우저 열기"
    bl_description = "서버에 웹소켓으로 연결하고, 웹 브라우저에서 제어판을 엽니다."
    
    def execute(self, context):
        global status_message
        if websocket_client:
            self.report({'WARNING'}, "이미 연결되어 있습니다.")
            return {'CANCELLED'}
        
        uri = context.scene.costestimator_server_url
        try:
            base_address = uri.replace("ws://", "http://").replace("wss://", "").split("/ws/")[0]
            webbrowser.open(base_address)
        except Exception as e:
            self.report({'WARNING'}, f"웹 브라우저 열기 실패: {e}")

        status_message = "서버에 연결 시도 중..."
        run_websocket_in_thread(uri)
        return {'FINISHED'}

class COSTESTIMATOR_OT_Disconnect(bpy.types.Operator):
    bl_idname = "costestimator.disconnect"
    bl_label = "연결 끊기 및 서버 종료"
    bl_description = "웹소켓 연결을 끊고, 실행 중인 로컬 서버도 함께 종료합니다."

    def execute(self, context):
        global websocket_client, status_message, websocket_thread_loop
        
        if websocket_client:
            if websocket_thread_loop:
                asyncio.run_coroutine_threadsafe(websocket_client.close(), websocket_thread_loop)
            websocket_client = None
            websocket_thread_loop = None
            status_message = "연결이 끊어졌습니다."
        else:
            self.report({'INFO'}, "웹소켓이 연결되어 있지 않습니다.")

        stop_server_process()
        self.report({'INFO'}, "서버가 종료되었습니다.")
        
        return {'FINISHED'}


class COSTESTIMATOR_PT_Panel(bpy.types.Panel):
    bl_label = "Cost Estimator"
    bl_idname = "COSTESTIMATOR_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Cost Estimator'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="서버 관리")
        
        row = box.row()
        row.active = server_process is None or server_process.poll() is not None
        row.operator("costestimator.start_server", text="서버 시작", icon='PLAY')

        box.label(text=f"서버 상태: {server_status}")

        box = layout.box()
        box.label(text="웹소켓 연결")
        box.prop(scene, "costestimator_server_url")
        
        split = box.split(factor=0.5, align=True)
        
        col1 = split.column()
        col1.active = server_status == "실행 중" and websocket_client is None
        col1.operator("costestimator.connect", text="연결 및 브라우저 열기", icon='LINKED')
        
        col2 = split.column()
        col2.operator("costestimator.disconnect", text="연결 끊기 & 서버 종료", icon='UNLINKED')
        
        box.label(text=f"웹소켓 상태: {status_message}")


classes = (
    COSTESTIMATOR_OT_StartServer,
    COSTESTIMATOR_OT_Connect,
    COSTESTIMATOR_OT_Disconnect,
    COSTESTIMATOR_PT_Panel
)

def register():
    # 라이브러리 경로 설정 코드는 이미 파일 최상단으로 이동했습니다.
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.costestimator_server_url = bpy.props.StringProperty(
        name="서버 주소", default="ws://127.0.0.1:8000/ws/blender-connector/"
    )
    bpy.app.timers.register(process_event_queue_timer)

def unregister():
    stop_server_process()

    if bpy.app.timers.is_registered(process_event_queue_timer):
        bpy.app.timers.unregister(process_event_queue_timer)
    
    global websocket_client, websocket_thread_loop
    if websocket_client and websocket_thread_loop:
        asyncio.run_coroutine_threadsafe(websocket_client.close(), websocket_thread_loop)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.costestimator_server_url

if __name__ == "__main__":
    register()