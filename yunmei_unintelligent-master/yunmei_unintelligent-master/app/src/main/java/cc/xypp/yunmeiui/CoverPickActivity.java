package cc.xypp.yunmeiui;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Bundle;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

import cc.xypp.yunmeiui.utils.ToastUtil;

/**
 * 「卡片封面」设置页（全透明）：从桌面卡片右下角的小齿轮进入。
 *
 *  - 未设置封面：直接打开系统相册选择图片；
 *  - 已设置封面：弹窗三选（更换图片 / 恢复默认图标 / 取消）。
 *
 * 选中的图片会等比采样到长边 ≤300px 再存入应用私有目录——RemoteViews 里塞
 * Bitmap 有 Binder 事务大小限制，直接塞原图会导致小部件渲染失败。
 * 图片只保存在本机应用私有目录，不上传任何服务器。
 */
public class CoverPickActivity extends Activity {

    private static final int REQ_PICK = 41;
    /** 封面最长边（px）。1×1 卡片实际渲染远小于此，留缩放余量并保证远低于 Binder 上限 */
    private static final int COVER_MAX_SIDE = 300;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        File existing = coverFile();
        if (existing.exists() && existing.length() > 0) {
            new AlertDialog.Builder(this)
                    .setTitle("卡片封面")
                    .setItems(
                            new CharSequence[]{"更换图片", "恢复默认图标", "取消"},
                            (dialog, which) -> {
                                if (which == 0) {
                                    openPicker();
                                } else if (which == 1) {
                                    //noinspection ResultOfMethodCallIgnored
                                    coverFile().delete();
                                    UnlockWidgetProvider.refreshAll(this);
                                    ToastUtil.show(this, "已恢复默认图标");
                                    finish();
                                } else {
                                    finish();
                                }
                            })
                    .setOnCancelListener(dialog -> finish())
                    .show();
        } else {
            openPicker();
        }
    }

    private void openPicker() {
        try {
            Intent i = new Intent(Intent.ACTION_GET_CONTENT);
            i.addCategory(Intent.CATEGORY_OPENABLE);
            i.setType("image/*");
            startActivityForResult(Intent.createChooser(i, "选择卡片封面"), REQ_PICK);
        } catch (Exception e) {
            ToastUtil.show(this, "没有找到可以选图的应用");
            finish();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK || resultCode != RESULT_OK
                || data == null || data.getData() == null) {
            finish();
            return;
        }
        if (!saveCover(data.getData())) {
            ToastUtil.show(this, "图片读取失败，换一张试试");
            finish();
            return;
        }
        UnlockWidgetProvider.refreshAll(this);
        ToastUtil.show(this, "封面已更新");
        finish();
    }

    private File coverFile() {
        return new File(getFilesDir(), "widget_cover.png");
    }

    private boolean saveCover(Uri uri) {
        try {
            // 第一遍只读尺寸，决定采样倍数，避免大图吃内存
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            InputStream is = getContentResolver().openInputStream(uri);
            if (is == null) return false;
            BitmapFactory.decodeStream(is, null, bounds);
            closeQuietly(is);
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return false;

            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inSampleSize = sampleSize(bounds.outWidth, bounds.outHeight);
            is = getContentResolver().openInputStream(uri);
            if (is == null) return false;
            Bitmap src = BitmapFactory.decodeStream(is, null, opts);
            closeQuietly(is);
            if (src == null) return false;

            Bitmap scaled = scaleDown(src, COVER_MAX_SIDE);
            FileOutputStream out = new FileOutputStream(coverFile());
            scaled.compress(Bitmap.CompressFormat.PNG, 100, out);
            out.flush();
            out.close();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private static int sampleSize(int width, int height) {
        int max = Math.max(width, height);
        int sample = 1;
        while (max / sample > COVER_MAX_SIDE * 2) {
            sample *= 2;
        }
        return sample;
    }

    /** 等比缩放，长边不超过 maxSide（拉伸/裁切交给小部件的 scaleType=centerCrop） */
    private static Bitmap scaleDown(Bitmap src, int maxSide) {
        int max = Math.max(src.getWidth(), src.getHeight());
        if (max <= maxSide) {
            return src;
        }
        float ratio = maxSide / (float) max;
        int w = Math.max(1, Math.round(src.getWidth() * ratio));
        int h = Math.max(1, Math.round(src.getHeight() * ratio));
        return Bitmap.createScaledBitmap(src, w, h, true);
    }

    private static void closeQuietly(InputStream is) {
        try {
            is.close();
        } catch (Exception ignored) {
        }
    }
}
