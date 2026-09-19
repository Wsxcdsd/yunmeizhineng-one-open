package cc.xypp.yunmeiui;

import static cc.xypp.yunmeiui.utils.HexUtil.hex2String;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;

import com.clj.fastble.BleManager;

import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

import cc.xypp.yunmeiui.eneity.Lock;
import cc.xypp.yunmeiui.function.UnlockService;
import cc.xypp.yunmeiui.utils.LockManageUtil;
import cc.xypp.yunmeiui.utils.ToastUtil;

/**
 * 桌面小部件点击后启动的「隐形中转页」。
 *
 * 效果：用户点击桌面小部件后，屏幕上什么都不出现（仍然停留在桌面），
 * 只用 Toast 汇报关键进度（连接失败 / 开门完成 / 电量等），完成后自动消失。
 *
 * 为什么需要它：UnlockService 的权限申请逻辑依赖 Activity（第一次使用时
 * 要弹蓝牙权限对话框），小部件/广播没法弹权限框；用一个完全透明的
 * Activity 来承载，既能原样复用 App 里全部开门逻辑，又不会「跳进应用界面」。
 *
 * 配套的 manifest 属性（缺一不可）：
 *   android:theme="@style/Theme.UnlockInvisible"  完全透明、无转场动画
 *   android:taskAffinity=""                       独立任务，不把 App 的界面带出来
 *   android:excludeFromRecents="true"             不进最近任务
 *   android:noHistory="true"                      离开即销毁
 */
public class UnlockActivity extends Activity {

    public static final String ACTION_WIDGET_UNLOCK = "cc.xypp.yunmeiui.WIDGET_UNLOCK";
    /** 可选：传入 Lock.toString() 序列化串指定开哪把锁；不传则用 App 里的默认锁 */
    public static final String EXTRA_LOCK = "lock";

    /** 防止连点两下触发两条并发蓝牙流程（并发连接同一把锁会互相干扰） */
    private static final AtomicBoolean RUNNING = new AtomicBoolean(false);

    private UnlockService unlockService;
    private boolean counted = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        if (!RUNNING.compareAndSet(false, true)) {
            toast("正在开门，请稍候");
            finish();
            return;
        }
        counted = true;

        BleManager.getInstance().init(getApplication());
        if (!BleManager.getInstance().isSupportBle()) {
            toast("设备不支持蓝牙");
            finish();
            return;
        }

        Lock currentLock = null;
        String lockData = getIntent().getStringExtra(EXTRA_LOCK);
        if (lockData != null && !lockData.equals("")) {
            currentLock = new Lock(lockData);
        }
        LockManageUtil lockManageUtil = new LockManageUtil(this);
        if (currentLock == null) {
            currentLock = lockManageUtil.getDef();
        }
        if (currentLock == null) {
            List<Lock> locks = lockManageUtil.getAll();
            if (!locks.isEmpty()) currentLock = locks.get(0);
        }
        if (currentLock == null
                || currentLock.D_CHAR == null || currentLock.D_CHAR.equals("")
                || currentLock.D_SERV == null || currentLock.D_SERV.equals("")) {
            toast("还没有可用的门锁，请先打开 App 登录并添加门锁");
            finish();
            return;
        }

        SharedPreferences sp = getSharedPreferences("storage", MODE_PRIVATE);
        // 与 App 主界面一致：设置里的「快速连接」开关对小部件同样生效
        boolean quickConnect = sp.getBoolean("quickCon", true);
        final Lock lock = currentLock;

        unlockService = new UnlockService(this, new UnlockService.Callback() {
            @Override
            public void setpss(int pss, String tip, boolean toast) {
                // 只把重要的消息弹出来，避免刷屏：失败类消息 toast=true，由这里过滤
                if (toast) toast(tip);
            }

            @Override
            public void start() {
            }

            @Override
            public void end() {
                // 成功/失败最终都会走到这里；延时一点退出，让最后的 Toast 显示完
                delayedFinish();
            }

            @Override
            public void successed() {
                toast("开门完成");
            }

            @Override
            public void result(byte[] data) {
                // 电量解析与 MainActivity 完全一致（AA 直接是数字，AB 按公式换算）
                int posAB = -1, posAA = -1;
                for (int i = 0; i < data.length; i++) {
                    if (data[i] == (byte) 0xAA) {
                        posAA = i;
                    } else if (data[i] == (byte) 0xAB) {
                        posAB = i;
                    }
                }
                int aa = posAA == -1 ? -1 : safeParse(hex2String(data, posAA + 1, 2));
                int ab = posAB == -1 ? -1 : safeParse(hex2String(data, posAB + 1, 2));
                int battery = aa;
                if (ab != -1) {
                    battery = (int) Math.round(100.0 * (ab - 40) / 24);
                }
                toast(String.format("电量:%d%%", battery));
            }
        }, lock, quickConnect);

        // openDoorWork 里有 sleep（等蓝牙开启），和 MainActivity 一样放到子线程
        new Thread(unlockService::openDoorWork).start();
    }

    private static int safeParse(String s) {
        try {
            return Integer.parseInt(s.trim());
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    private void toast(String tip) {
        runOnUiThread(() -> ToastUtil.show(getApplicationContext(), tip));
    }

    private void delayedFinish() {
        new Handler(Looper.getMainLooper())
                .postDelayed(UnlockActivity.this::finish, 1500);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        // 第一次使用时权限没批，UnlockService 会调 requestPermissions 弹系统对话框；
        // 授权结果原样转发回去，它自己会重新走一遍开门流程（与 MainActivity 相同）
        if (unlockService != null) {
            unlockService.onRequestPermissionsResult(requestCode, permissions, grantResults);
        }
    }

    @Override
    protected void onDestroy() {
        if (counted) {
            RUNNING.set(false);
        }
        super.onDestroy();
    }
}
