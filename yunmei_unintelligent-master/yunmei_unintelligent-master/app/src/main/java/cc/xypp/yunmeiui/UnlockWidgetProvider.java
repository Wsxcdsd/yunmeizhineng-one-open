package cc.xypp.yunmeiui;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.widget.RemoteViews;

import java.util.List;

import cc.xypp.yunmeiui.eneity.Lock;
import cc.xypp.yunmeiui.utils.LockManageUtil;

/**
 * 桌面「一键开门」小部件。
 *
 * 点击小部件 = 启动透明的 UnlockActivity → 蓝牙开门 → 全程只有 Toast 提示，
 * 不会打开应用界面，也不会从桌面跳走。
 *
 * 注意：小部件上显示的锁名只是「放置/刷新时的快照」，
 * 实际开哪把锁以点击瞬间的 App 默认锁为准（在 App 里长按门锁可设为默认）。
 */
public class UnlockWidgetProvider extends AppWidgetProvider {

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager, int[] appWidgetIds) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.unlock_widget);
        views.setTextViewText(R.id.widget_lock_name, currentLockName(context));

        // 点击整个小部件 = 开门
        Intent intent = new Intent(context, UnlockActivity.class);
        intent.setAction(UnlockActivity.ACTION_WIDGET_UNLOCK);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                context, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widget_root, pendingIntent);

        appWidgetManager.updateAppWidget(appWidgetIds, views);
    }

    /** 展示用的锁名：默认锁 → 第一把锁 → 提示语 */
    private String currentLockName(Context context) {
        try {
            LockManageUtil util = new LockManageUtil(context);
            Lock def = util.getDef();
            if (def != null && !def.label.equals("")) return def.label;
            List<Lock> locks = util.getAll();
            if (!locks.isEmpty()) return locks.get(0).label;
        } catch (Exception ignored) {
        }
        return "未添加门锁";
    }
}
