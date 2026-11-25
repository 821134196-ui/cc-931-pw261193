#!/bin/sh
# 演示作业：根进程派生 worker 后，父进程先退出，留下被 subreaper 收养的孤儿。
# 所有进程持续存活，便于在页面上观察树形归属；停止由守护进程按身份下发信号。
set -u

echo "[root $$] 作业启动，派生 worker"

# 普通 worker：一直存活
(
  while :; do sleep 1; done
) &
echo "[root $$] worker pid=$!"

# 父进程先退出的场景：parent 派生 child 后停留片刻（确保托管端先登记完整链），
# 随后 parent 退出，child 被托管守护（subreaper）收养，启动链断裂 -> 页面标"疑似"
(
  PARENT=$$
  (
    # child：父退出后 reparent 给 subreaper，启动链在此断裂 -> 页面标"疑似"
    while :; do sleep 1; done
  ) &
  CHILD=$!
  echo "[parent $PARENT] 已派生 child pid=$CHILD，parent 停留 2s 后先退出"
  sleep 2
  exit 0
) &

# 根进程保活，等待托管端停止
while :; do
  sleep 1
done
