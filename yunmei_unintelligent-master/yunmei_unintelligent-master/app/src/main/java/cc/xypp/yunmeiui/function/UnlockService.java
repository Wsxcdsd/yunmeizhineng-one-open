package cc.xypp.yunmeiui.function;

/*
 * ★ 桌面卡片 v2 改动说明（相对原仓库 UnlockService.java，逻辑零改动，只有 3 处适配）：
 *   1. 构造参数 Activity → Context：桌面卡片走「后台广播」路径时没有 Activity 可传，
 *      而日常开门路径（权限齐全）其实完全用不到 Activity；
 *   2. 两处 ActivityCompat.requestPermissions 前加 instanceof Activity 守卫：
 *      弹系统权限框只可能发生在 Activity 环境（App 内 / 透明中转页）；
 *      后台广播路径在调用前已自查权限，权限不齐根本不会进来，这里是双保险；
 *   3. 开锁报文、扫描/连接/学习地址等全部逻辑与原版逐行一致。
 */

import static java.lang.Thread.sleep;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothGatt;
import android.content.Context;
import android.content.pm.PackageManager;

import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.clj.fastble.BleManager;
import com.clj.fastble.callback.BleGattCallback;
import com.clj.fastble.callback.BleNotifyCallback;
import com.clj.fastble.callback.BleScanAndConnectCallback;
import com.clj.fastble.callback.BleWriteCallback;
import com.clj.fastble.data.BleDevice;
import com.clj.fastble.exception.BleException;
import com.clj.fastble.scan.BleScanRuleConfig;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;

import cc.xypp.yunmeiui.eneity.Lock;
import cc.xypp.yunmeiui.utils.LockManageUtil;

public class UnlockService {

    public static abstract class Callback {
        public void setpss(int pss, String tip) {
            setpss(pss, tip, false);
        }

        ;

        public abstract void setpss(int pss, String tip, boolean toast);

        public abstract void start();

        public abstract void end();

        public abstract void successed();

        public abstract void result(byte[] data);
    }

    private class bleNotifyCallback extends BleNotifyCallback {
        Callback callback;

        bleNotifyCallback(Callback _callback) {
            super();
            callback = _callback;
        }

        @Override
        public void onNotifySuccess() {
            callback.setpss(50, "连接成功");
            sendMsg();
        }

        @Override
        public void onNotifyFailure(BleException exception) {
        }

        @Override
        public void onCharacteristicChanged(byte[] data) {
            callback.result(data);
        }
    }

    private final String[] permission = new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION};

    private BleDevice connectedDevice;

    private final Context ctx;
    private final Callback callback;
    private final Lock currentLock;
    private boolean quickConnect;

    public UnlockService(Context context, Callback _callback, Lock _currentLock, boolean _quickConnect) {
        ctx = context;
        callback = _callback;
        currentLock = _currentLock;
        quickConnect = _quickConnect;
    }


    public void openDoorWork() {
        callback.start();
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            String[] genPms = new String[]{Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.BLUETOOTH_CONNECT};
            List<String> denyPermissions = new ArrayList<>();
            for (String value : genPms) {
                if (ContextCompat.checkSelfPermission(ctx, value) != PackageManager.PERMISSION_GRANTED) {//判断权限是否已授权
                    //没有权限 就添加
                    denyPermissions.add(value);
                }
            }
            if (!denyPermissions.isEmpty()) {
                //申请权限授权（弹系统权限框只可能发生在 Activity 环境；
                //后台广播路径在进来之前已自查过权限，这里只是双保险）
                if (ctx instanceof Activity) {
                    ActivityCompat.requestPermissions((Activity) ctx, denyPermissions.toArray(new String[denyPermissions.size()]), 105);
                    return;
                }
                callback.setpss(0, "权限不足，请先打开应用授权", true);
                callback.end();
                return;
            }
        }
        BluetoothAdapter blueadapter = BluetoothAdapter.getDefaultAdapter();
        if (!blueadapter.isEnabled()) {
            callback.setpss(13, "开启蓝牙...");
            if (!blueadapter.enable()) {
                callback.setpss(0, "蓝牙没有启用", true);
                callback.end();
                return;
            }
            callback.setpss(15, "等待蓝牙...");
            try {
                for (int i = 0; i < 5; i++) {
                    sleep(2000);
                    if (blueadapter.isEnabled()) {
                        break;
                    } else if (i == 4) {
                        callback.setpss(0, "蓝牙没有启用", true);
                        callback.end();
                        return;
                    }
                }
            } catch (InterruptedException e) {
                e.printStackTrace();
            }
        }
        if (!currentLock.D_Mac.equals("") && quickConnect) {
            callback.setpss(20, "开始快速连接");
            connect(currentLock.D_Mac);
        } else {
            callback.setpss(20, "开始扫描");
            scan();
        }
    }

    public void stop() {

    }

    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        if (requestCode == 100) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                scan();
            } else {
                callback.setpss(0, "您拒绝了权限请求", true);
                callback.end();
            }
        } else if (requestCode == 105) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                openDoorWork();
            } else {
                callback.setpss(0, "您拒绝了权限请求", true);
                callback.end();
            }
        }
    }

    private void scan() {
        List<String> denyPermissions = new ArrayList<>();
        for (String value : permission) {
            if (ContextCompat.checkSelfPermission(ctx, value) != PackageManager.PERMISSION_GRANTED) {//判断权限是否已授权
                //没有权限 就添加
                denyPermissions.add(value);
            }
        }
        if (!denyPermissions.isEmpty()) {
            //申请权限授权（同上：仅 Activity 环境能弹框，后台广播路径进来前已自查权限）
            if (ctx instanceof Activity) {
                ActivityCompat.requestPermissions((Activity) ctx, denyPermissions.toArray(new String[denyPermissions.size()]), 100);
                return;
            }
            callback.setpss(0, "权限不足，请先打开应用授权", true);
            callback.end();
            return;
        }

        UUID[] uuids = {UUID.fromString(currentLock.D_SERV)};
        System.out.println(Arrays.toString(uuids));
        BleScanRuleConfig scanRuleConfig = new BleScanRuleConfig.Builder()
                .setServiceUuids(uuids)
                .build();
        BleManager bleManager = BleManager.getInstance();
        bleManager.initScanRule(scanRuleConfig);
        bleManager.scanAndConnect(new BleScanAndConnectCallback() {
            @Override
            public void onScanStarted(boolean success) {
                if (!success) {
                    callback.setpss(0, "设备未找到", true);
                    callback.end();
                }
            }

            @Override
            public void onScanning(BleDevice bleDevice) {
                System.out.println(bleDevice);
            }

            @Override
            public void onScanFinished(BleDevice scanResult) {

                // 扫描结束，结果即为扫描到的第一个符合扫描规则的BLE设备，如果为空表示未搜索到（主线程）
                if (scanResult == null) {
                    callback.setpss(0, "设备未找到", true);
                    callback.end();
                } else callback.setpss(30, "设备已找到");
            }

            @Override
            public void onStartConnect() {
                callback.setpss(43, "正在连接");
                // 开始连接（主线程）
            }

            @Override
            public void onConnectFail(BleDevice bleDevice, BleException exception) {
                callback.setpss(0, "连接失败", true);
                callback.end();
            }

            @Override
            public void onConnectSuccess(BleDevice bleDevice, BluetoothGatt gatt, int status) {
                connectedDevice = bleDevice;
                callback.setpss(40, "连接成功");
                BleManager.getInstance().notify(bleDevice, currentLock.D_SERV, currentLock.D_CHAR.replace("6E400002", "6E400003"), new bleNotifyCallback(callback));
            }

            @Override
            public void onDisConnected(boolean isActiveDisConnected, BleDevice device, BluetoothGatt gatt, int status) {
                // 连接断开，isActiveDisConnected是主动断开还是被动断开（主线程）
            }
        });
    }

    private void connect(String Mac) {
        BleManager.getInstance().connect(Mac, new BleGattCallback() {
            @Override
            public void onStartConnect() {
            }

            @Override
            public void onConnectFail(BleDevice bleDevice, BleException exception) {
                quickConnect = false;
                callback.setpss(0, "快速连接失败，使用常规模式尝试");
                scan();
            }

            @Override
            public void onConnectSuccess(BleDevice bleDevice, BluetoothGatt gatt, int status) {
                connectedDevice = bleDevice;
                callback.setpss(40, "连接成功");
                BleManager.getInstance().notify(bleDevice, currentLock.D_SERV, currentLock.D_CHAR.replace("6E400002", "6E400003"), new bleNotifyCallback(callback));
            }

            @Override
            public void onDisConnected(boolean isActiveDisConnected, BleDevice bleDevice, BluetoothGatt gatt, int status) {

            }
        });
    }

    private void sendMsg() {
        BleManager.getInstance().write(
                connectedDevice,
                currentLock.D_SERV,
                currentLock.D_CHAR,
                getPwd(currentLock.D_SEC),
                new BleWriteCallback() {
                    @Override
                    public void onWriteSuccess(int current, int total, byte[] justWrite) {
                        if (current == total) {
                            callback.setpss(100, "开门完成");
                            if (!quickConnect) {
                                currentLock.D_Mac = connectedDevice.getMac();
                                LockManageUtil lockManageUtil = new LockManageUtil(ctx);
                                lockManageUtil.setMac(currentLock.label, currentLock.D_Mac);
                            }
                            callback.end();
                            callback.successed();
                        } else {
                            callback.setpss(75, "正在发送数据");
                        }
                    }

                    @Override
                    public void onWriteFailure(BleException exception) {
                        callback.setpss(0, "开门失败", true);
                        callback.end();
                    }
                });
    }

    private byte[] getPwd(String secret) {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int pw = (int) Math.floor(Math.random() * 1000000);
        bos.write(208);
        int len = secret.length() + 2 + 2 + 10;
        bos.write(len);
        byte[] a = secret.getBytes();
        for (byte sa : a) {
            bos.write(sa);
        }
        bos.write(165);
        for (int i = 0; i < 6; i++) {
            bos.write((pw % 10));
            pw /= 10;
        }
        bos.write(73);
        bos.write(68);
        bos.write(48);
        bos.write(49);

        bos.write(167);
        return bos.toByteArray();
    }


}
