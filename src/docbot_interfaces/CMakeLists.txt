cmake_minimum_required(VERSION 3.8)
project(docbot_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(std_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

set(msg_files
  msg/AudioFrame.msg
  msg/TriggerEvent.msg
  msg/DialogState.msg
  msg/EscalationEvent.msg
)

set(srv_files
  srv/DumpAudioBuffer.srv
  srv/QueryLLM.srv
  srv/AppendHealthLog.srv
  srv/Escalate.srv
  srv/SetMicGate.srv
)

set(action_files
  action/Transcribe.action
  action/Speak.action
)

rosidl_generate_interfaces(${PROJECT_NAME}
  ${msg_files}
  ${srv_files}
  ${action_files}
  DEPENDENCIES builtin_interfaces std_msgs
)

ament_export_dependencies(rosidl_default_runtime)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
