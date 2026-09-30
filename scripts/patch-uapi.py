#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 rsuntk/KernelSU 的内核侧补上官方管理器要求的 uapi_version 字段。

======================================================================
背景（已用官方源码逐行证实，非推测）
======================================================================

官方 tiann 管理器 v3.3.0 的 manager/app/src/main/cpp/ksu.cc：

    struct ksu_get_info_cmd get_info() {
        if (!g_version.version) {
            if (ksuctl(KSU_IOCTL_GET_INFO, &g_version) < 0) {      // 4 字段结构, size=16
                ksuctl(KSU_IOCTL_GET_INFO_LEGACY, &g_version);     // 3 字段结构, size=0
                g_version.uapi_version = 0;                        // ★ 回退即置 0
            }
        }
        return g_version;
    }

官方 v3.3.0 的两个 ioctl 号（uapi/supercall.h）：

    KSU_IOCTL_GET_INFO        = _IOR('K', 2, struct ksu_get_info_cmd);   // size=16
    KSU_IOCTL_GET_INFO_LEGACY = _IOC(_IOC_READ, 'K', 2, 0);              // size=0

而 rsuntk 的（v3.2.2-10-legacy 与 main 都一样）：

    KSU_IOCTL_GET_INFO        = _IOC(_IOC_READ, 'K', 2, 0);              // size=0 ★

⇒ rsuntk 的 GET_INFO 号 == 官方的 GET_INFO_LEGACY 号。
   于是管理器的新调用必然 -ENOTTY、必然走回退分支、uapi_version 必然被写成 0。

管理器侧判据（Natives.kt）：

    fun checkUAPIMismatch() = kernelUAPIVersion != managerUAPIVersion   // 必须严格相等
    fun requireNewKernel()  = (version != -1 && version < 32513) || checkUAPIMismatch()

HomeUiState.kt：

    showRequireKernelWarning = isManager && requiresNewKernel
    showUAPIMisMatchWarning  = isManager && showRequireKernelWarning && uapiMismatch

⇒ requiresNewKernel 同时控制两条红框。内核 version 已是 32601（>= 32513），
  第一条不成立，但 checkUAPIMismatch() = (0 != 2) = true，所以两条红框同时出现。
  （这也解释了文案为何自相矛盾：显示「版本 32601 过低…升级至 32513」，
   真实原因其实是 uapi，是管理器文案缺陷。）

⇒ 只要内核上报 uapi_version == 2，checkUAPIMismatch() 为 false，
  requiresNewKernel 为 false，两条红框同时消失。

======================================================================
本脚本做三件事（每处都带断言，未命中即非零退出，绝不静默通过）
======================================================================
  ① struct ksu_get_info_cmd 补第 4 个字段 uapi_version；
     并新增 3 字段的 struct ksu_get_info_legacy_cmd
  ② KSU_IOCTL_GET_INFO 改为 _IOR('K', 2, struct ksu_get_info_cmd)（与官方号一致）；
     同时保留 _IOC(_IOC_READ, 'K', 2, 0) 作为 KSU_IOCTL_GET_INFO_LEGACY
     —— 这一步很关键：设备上现存的旧 ksud 用的是 size=0 的号，必须继续可用，
        否则刷入新内核后 ksud 会在开机阶段失效。
  ③ dispatch.c：do_get_info 上报 uapi_version；新增 do_get_info_legacy
     并注册进 ksu_ioctl_handlers 分发表（perm_check 与 GET_INFO 一致 = always_allow）

用法：
    patch-uapi.py <drivers/kernelsu 目录>
"""

import os
import sys


def die(msg):
    print('[FAIL] ' + msg)
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        die('用法: patch-uapi.py <drivers/kernelsu 目录>')

    ksu = sys.argv[1].rstrip('/\\')
    if not os.path.isdir(ksu):
        die('目录不存在: ' + ksu)

    hdr = os.path.join(ksu, 'include', 'uapi', 'supercall.h')
    dsp = os.path.join(ksu, 'supercall', 'dispatch.c')
    for f in (hdr, dsp):
        if not os.path.isfile(f):
            die('找不到文件: ' + f)

    # ------------------------------------------------------------------
    # ① + ② supercall.h
    # ------------------------------------------------------------------
    s = open(hdr, encoding='utf-8', errors='replace').read()

    old_struct = (
        "struct ksu_get_info_cmd {\n"
        "    __u32 version; /* Output: KERNEL_SU_VERSION */\n"
        "    __u32 flags; /* Output: KSU_GET_INFO_FLAG_* bits */\n"
        "    __u32 features; /* Output: max feature ID supported */\n"
        "};"
    )
    new_struct = (
        "/* patched by CI: 官方管理器按 4 字段读取，缺此字段会被当成 uapi 0 */\n"
        "DECLARE(__u32, KERNEL_SU_UAPI_VERSION, 2);\n"
        "\n"
        "struct ksu_get_info_cmd {\n"
        "    __u32 version; /* Output: KERNEL_SU_VERSION */\n"
        "    __u32 flags; /* Output: KSU_GET_INFO_FLAG_* bits */\n"
        "    __u32 features; /* Output: max feature ID supported */\n"
        "    __u32 uapi_version; /* Output: KERNEL_SU_UAPI_VERSION */\n"
        "};\n"
        "\n"
        "/* patched by CI: 3 字段版本，供旧 ksud / 旧管理器继续可用 */\n"
        "struct ksu_get_info_legacy_cmd {\n"
        "    __u32 version;\n"
        "    __u32 flags;\n"
        "    __u32 features;\n"
        "};"
    )
    n = s.count(old_struct)
    if n != 1:
        die('supercall.h 结构体补丁未命中（命中 %d 处，期望 1）' % n)
    s = s.replace(old_struct, new_struct)

    old_ioctl = "DECLARE(__u32, KSU_IOCTL_GET_INFO, _IOC(_IOC_READ, 'K', 2, 0));"
    new_ioctl = (
        "DECLARE(__u32, KSU_IOCTL_GET_INFO, _IOR('K', 2, struct ksu_get_info_cmd));\n"
        "/* patched by CI: 保留 size=0 的旧号，兼容旧 ksud */\n"
        "DECLARE(__u32, KSU_IOCTL_GET_INFO_LEGACY, _IOC(_IOC_READ, 'K', 2, 0));"
    )
    n = s.count(old_ioctl)
    if n != 1:
        die('supercall.h ioctl 补丁未命中（命中 %d 处，期望 1）' % n)
    s = s.replace(old_ioctl, new_ioctl)

    open(hdr, 'w', encoding='utf-8').write(s)
    print('[OK] supercall.h 已打补丁')

    # ------------------------------------------------------------------
    # ③ dispatch.c
    # ------------------------------------------------------------------
    t = open(dsp, encoding='utf-8', errors='replace').read()

    old_set = "    cmd.features = KSU_FEATURE_MAX;\n"
    new_set = (
        "    cmd.features = KSU_FEATURE_MAX;\n"
        "    /* patched by CI: 上报 uapi 版本，与官方管理器 (uapi 2) 对齐 */\n"
        "    cmd.uapi_version = KERNEL_SU_UAPI_VERSION;\n"
    )
    n = t.count(old_set)
    if n != 1:
        die('dispatch.c do_get_info 补丁未命中（命中 %d 处，期望 1）' % n)
    t = t.replace(old_set, new_set)

    legacy_fn = (
        "/* patched by CI: 3 字段 legacy 版本，兼容旧 ksud / 旧管理器 */\n"
        "static int do_get_info_legacy(void __user *arg)\n"
        "{\n"
        "    struct ksu_get_info_legacy_cmd cmd = { .version = KERNEL_SU_VERSION, .flags = 0 };\n"
        "\n"
        "    if (is_manager()) {\n"
        "        cmd.flags |= KSU_GET_INFO_FLAG_MANAGER;\n"
        "    }\n"
        "\n"
        "    cmd.features = KSU_FEATURE_MAX;\n"
        "\n"
        "    if (copy_to_user(arg, &cmd, sizeof(cmd))) {\n"
        '        pr_err("get_version: copy_to_user failed\\n");\n'
        "        return -EFAULT;\n"
        "    }\n"
        "\n"
        "    return 0;\n"
        "}\n"
        "\n"
    )
    anchor = "// IOCTL handlers mapping table"
    n = t.count(anchor)
    if n != 1:
        die('dispatch.c 未找到 handler 表锚点（命中 %d 处，期望 1）' % n)
    t = t.replace(anchor, legacy_fn + anchor)

    old_entry = (
        "    {\n"
        "        .cmd = KSU_IOCTL_GET_INFO,\n"
        '        .name = "GET_INFO",\n'
        "        .handler = do_get_info,\n"
        "        .perm_check = always_allow\n"
        "    },"
    )
    new_entry = old_entry + "\n" + (
        "    {\n"
        "        .cmd = KSU_IOCTL_GET_INFO_LEGACY,\n"
        '        .name = "GET_INFO_LEGACY",\n'
        "        .handler = do_get_info_legacy,\n"
        "        .perm_check = always_allow\n"
        "    },"
    )
    n = t.count(old_entry)
    if n != 1:
        die('dispatch.c 分发表补丁未命中（命中 %d 处，期望 1）' % n)
    t = t.replace(old_entry, new_entry)

    open(dsp, 'w', encoding='utf-8').write(t)
    print('[OK] dispatch.c 已打补丁')

    # ------------------------------------------------------------------
    # 复验
    # ------------------------------------------------------------------
    h = open(hdr, encoding='utf-8').read()
    d = open(dsp, encoding='utf-8').read()
    checks = [
        ('supercall.h 定义 KERNEL_SU_UAPI_VERSION = 2', 'KERNEL_SU_UAPI_VERSION, 2' in h),
        ('supercall.h 结构体含 uapi_version 字段', 'uapi_version; /* Output' in h),
        ('supercall.h 新增 legacy 结构体', 'ksu_get_info_legacy_cmd' in h),
        ('supercall.h GET_INFO 号已改为 _IOR', "_IOR('K', 2, struct ksu_get_info_cmd)" in h),
        ('supercall.h 保留 GET_INFO_LEGACY 号', 'KSU_IOCTL_GET_INFO_LEGACY' in h),
        ('dispatch.c 上报 uapi_version', 'cmd.uapi_version = KERNEL_SU_UAPI_VERSION;' in d),
        ('dispatch.c 新增 do_get_info_legacy', 'do_get_info_legacy(void __user *arg)' in d),
        ('dispatch.c 注册 GET_INFO_LEGACY 表项', 'KSU_IOCTL_GET_INFO_LEGACY' in d),
    ]
    for name, ok in checks:
        print(('  [OK] ' if ok else '  [!!] ') + name)
    bad = [name for name, ok in checks if not ok]
    if bad:
        die('复验未通过: ' + '; '.join(bad))

    print('[DONE] uapi 补丁全部通过复验')


if __name__ == '__main__':
    main()
