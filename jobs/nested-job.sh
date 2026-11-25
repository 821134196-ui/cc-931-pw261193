#!/bin/sh
# 多层级演示作业：root -> A -> B -> C 三层 worker，用于观察完整启动链。
set -u
(
  (
    (
      while :; do sleep 1; done
    ) &
    while :; do sleep 1; done
  ) &
  while :; do sleep 1; done
) &
echo "[root $$] 三层 worker 树已建立"
while :; do
  sleep 1
done
