package cc.xypp.yunmeiui;

import static cc.xypp.yunmeiui.utils.HexUtil.hex2String;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.RemoteViews;

import androidx.core.content.ContextCompat;

import com.clj.fastble.BleManager;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

import cc.xypp.yunmeiui.eneity.Lock;
import cc.xypp.yunmeiui.function.UnlockService;
import cc.xypp.yunmeiui.utils.LockManageUtil;
import cc.xypp.yunmeiui.utils.ToastUtil;

/**
 * 桌面「一键开门」小部件。
 *
 * v2：点击改为发送广播（PendingIntent.getBroadcast），在后台直接完成开门——
 * 广播不启动任何 Activity，因此不会触发系统桌面的转场/流体云动效，
 * 用户只会看到 Toast 汇报。只有权限不齐（首次使用/权限被撤）时才转跳
 * 透明中转页 UnlockActivity 去弹系统权限框。
 *
 * v3：支持自定义卡片封面——长边 ≤300px 的图片存在应用私有目录
 * files/widget_cover.png（由 CoverPickActivity 写入），有图时铺满卡片并隐藏
 * 默认挂锁+锁名；没图时保持默认外观。右下角小齿轮 = 更换封面入口。
 */
public class UnlockWidgetProvider extends AppWidgetProvider {

    public static final String ACTION_WIDGET_UNLOCK = "cc.xypp.yunmeiui.WIDGET_UNLOCK";
    /** 防连点守卫：桌面卡片（广播路径）与透明中转页（Activity 路径）共用同一个 */
    public static final AtomicBoolean RUNNING = new AtomicBoolean(false);

    /** 自定义封面文件（CoverPickActivity 写入，这里读取） */
    private static final String COVER_FILE = "widget_cover.png";

    private static Bitmap coverCache;

    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager, int[] appWidgetIds) {
        appWidgetManager.updateAppWidget(appWidgetIds, buildRemoteViews(context));
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (ACTION_WIDGET_UNLOCK.equals(intent.getAction())) {
            unlockInPlace(context);
        }
    }

    /** 组装小部件外观：v2 的开门广播 + v3 的封面/齿轮入口都在这里 */
    public static RemoteViews buildRemoteViews(Context context) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.unlock_widget);

        Bitmap cover = loadCover(context);
        if (cover != null) {
            views.setImageViewBitmap(R.id.widget_cover, cover);
            views.setViewVisibility(R.id.widget_cover, View.VISIBLE);
            views.setViewVisibility(R.id.widget_default, View.GONE);
        } else {
            views.setViewVisibility(R.id.widget_cover, View.GONE);
            views.setViewVisibility(R.id.widget_default, View.VISIBLE);
            views.setTextViewText(R.id.widget_lock_name, currentLockName(context));
        }

        // 主体点击 = 开门广播（不触发系统转场动效）
        Intent unlock = new Intent(context, UnlockWidgetProvider.class);
        unlock.setAction(ACTION_WIDGET_UNLOCK);
        views.setOnClickPendingIntent(R.id.widget_root, PendingIntent.getBroadcast(
                context, 0, unlock,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        // v4：「更换封面」入口移到 App 设置页，卡片上不再有齿轮
        return views;
    }

    /** 封面变化后调用：让缓存失效并立即刷新桌面上所有卡片实例 */
    public static void refreshAll(Context context) {
        coverCache = null;
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(new ComponentName(context, UnlockWidgetProvider.class));
        if (ids.length > 0) {
            manager.updateAppWidget(ids, buildRemoteViews(context));
        }
    }

    private static Bitmap loadCover(Context context) {
        if (coverCache != null) {
            return coverCache;
        }
        File file = new File(context.getFilesDir(), COVER_FILE);
        if (!file.exists() || file.length() == 0) {
            return null;
        }
        Bitmap decoded = BitmapFactory.decodeFile(file.getAbsolutePath());
        if (decoded != null) {
            coverCache = decoded;
        }
        return decoded;
    }

    /** 后台直开：权限齐全时不启动任何界面，只用 Toast 汇报 */
    private void unlockInPlace(final Context context) {
        final Lock currentLock = pickLock(context);
        if (currentLock == null
                || currentLock.D_CHAR == null || currentLock.D_CHAR.equals("")
                || currentLock.D_SERV == null || currentLock.D_SERV.equals("")) {
            toast(context, "还没有可用的门锁，请先打开 App 登录并添加门锁");
            return;
        }
        if (permissionsMissing(context)) {
            // 首次使用/权限被撤：交给透明中转页弹系统权限框（仅这一次会有转场动效）
            Intent i = new Intent(context, UnlockActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(i);
            return;
        }
        if (!RUNNING.compareAndSet(false, true)) {
            toast(context, "正在开门，请稍候");
            return;
        }

        // FastBleLib 的 init 只收 Application；getApplicationContext() 的实际对象就是它
        BleManager.getInstance().init((android.app.Application) context.getApplicationContext());
        if (!BleManager.getInstance().isSupportBle()) {
            toast(context, "设备不支持蓝牙");
            RUNNING.set(false);
            return;
        }
        // 与 App 主界面一致：设置里的「快速连接」开关对小部件同样生效
        final boolean quickConnect = context.getSharedPreferences("storage", Context.MODE_PRIVATE)
                .getBoolean("quickCon", true);

        final UnlockService unlockService = new UnlockService(
                context.getApplicationContext(),
                new UnlockService.Callback() {
                    @Override
                    public void setpss(int pss, String tip, boolean toast) {
                        // 只把重要的消息弹出来：失败类消息 toast=true，由这里过滤
                        if (toast) toast(context, tip);
                    }

                    @Override
                    public void start() {
                    }

                    @Override
                    public void end() {
                        RUNNING.set(false);
                    }

                    @Override
                    public void successed() {
                        toast(context, "开门完成");
                    }

                    @Override
                    public void result(byte[] data) {
                        // 电量解析与 App 主界面完全一致（AA 直接是数字，AB 按公式换算）
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
                        toast(context, String.format("电量:%d%%", battery));
                    }
                },
                currentLock,
                quickConnect);

        // openDoorWork 里有 sleep（等蓝牙开启），必须放子线程（与 App 内一致）
        new Thread(unlockService::openDoorWork).start();
    }

    private static Lock pickLock(Context context) {
        LockManageUtil lockManageUtil = new LockManageUtil(context);
        Lock lock = lockManageUtil.getDef();
        if (lock == null) {
            List<Lock> locks = lockManageUtil.getAll();
            if (!locks.isEmpty()) lock = locks.get(0);
        }
        return lock;
    }

    private static String currentLockName(Context context) {
        Lock def = new LockManageUtil(context).getDef();
        if (def != null && !def.label.equals("")) {
            return def.label;
        }
        List<Lock> locks = new LockManageUtil(context).getAll();
        if (!locks.isEmpty()) {
            return locks.get(0).label;
        }
        return "未添加门锁";
    }

    /**
     * 权限自查：任何一项缺失都转跳透明中转页去弹框（广播自身无法弹权限框）。
     * 检查项与 UnlockService 内部会用到的保持一致（含扫描所需的定位权限）。
     */
    private static boolean permissionsMissing(Context context) {
        List<String> need = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            need.add(android.Manifest.permission.BLUETOOTH_SCAN);
            need.add(android.Manifest.permission.BLUETOOTH_CONNECT);
        }
        need.add(android.Manifest.permission.ACCESS_FINE_LOCATION);
        need.add(android.Manifest.permission.ACCESS_COARSE_LOCATION);
        for (String p : need) {
            if (ContextCompat.checkSelfPermission(context, p) != PackageManager.PERMISSION_GRANTED) {
                return true;
            }
        }
        return false;
    }

    private static int safeParse(String s) {
        try {
            return Integer.parseInt(s.trim());
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    /** Toast 必须在主线程弹（openDoorWork 的等待循环会在子线程回调进度） */
    private static void toast(final Context context, final String tip) {
        MAIN.post(() -> ToastUtil.show(context.getApplicationContext(), tip));
    }
}
