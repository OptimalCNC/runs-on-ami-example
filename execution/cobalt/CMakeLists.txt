cmake_minimum_required(VERSION 3.28)
project(xenomai_cobalt_example LANGUAGES C)
enable_testing()

set(XENOMAI_ROOT "/usr/xenomai" CACHE PATH "Installed Xenomai Cobalt prefix")
set(XENO_CONFIG "${XENOMAI_ROOT}/bin/xeno-config")
execute_process(COMMAND "${XENO_CONFIG}" --alchemy --cflags
  OUTPUT_VARIABLE XENOMAI_CFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
execute_process(COMMAND "${XENO_CONFIG}" --alchemy --ldflags
  OUTPUT_VARIABLE XENOMAI_LDFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
separate_arguments(XENOMAI_CFLAGS UNIX_COMMAND "${XENOMAI_CFLAGS}")
separate_arguments(XENOMAI_LDFLAGS UNIX_COMMAND "${XENOMAI_LDFLAGS}")

add_executable(cobalt main.c)
target_compile_options(cobalt PRIVATE -Wall -Wextra ${XENOMAI_CFLAGS})
# xeno-config emits ordered libraries and linker arguments, including its entry wrapper.
target_link_libraries(cobalt PRIVATE ${XENOMAI_LDFLAGS})
add_test(NAME cobalt COMMAND cobalt)
set_tests_properties(cobalt PROPERTIES TIMEOUT 20)
