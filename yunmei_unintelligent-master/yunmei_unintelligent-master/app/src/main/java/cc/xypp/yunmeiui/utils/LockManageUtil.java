package cc.xypp.yunmeiui.utils;

import android.content.Context;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import cc.xypp.yunmeiui.eneity.Lock;

/**
 * ★ 本文件与原版相比只改了一处（setMac，见方法内注释）：
 *   学习到新蓝牙地址时，同步更新「默认锁」的记录。
 *   原版只更新 locks 列表、不更新 lock_default，导致默认锁一直记着旧地址，
 *   快速连接永远失败、每次开门都退回 10 秒扫描。修复后默认锁也能秒连。
 */
public class LockManageUtil {
    SecureStorage ssp;
    public LockManageUtil(Context context){
        ssp = new SecureStorage(context);
    }
    public List<Lock> getAll() {
        Set<String> lockSet = ssp.getVal("locks", new HashSet<>());
        List<Lock> locks = new ArrayList<>();
        lockSet.forEach(v -> {
            locks.add(new Lock(v));
        });
        return locks;
    }
    public Lock getDef(){
        String lockDat = ssp.getVal("lock_default", "");
        if(lockDat.equals(""))return null;
        return new Lock(lockDat);
    }
    public void add(Lock lock) throws RuntimeException{
        List<Lock> locks = getAll();
        for (Lock existLock : locks) {
            if(existLock.label.equals(lock.label)){
                throw new RuntimeException(String.format("%s 已存在",lock.label));
            }
        }
        Set<String> finLocks = new HashSet<>();
        locks.forEach(lock1 -> finLocks.add(lock1.toString()));
        finLocks.add(lock.toString());
        ssp.setVal("locks",finLocks);
    }
    public void remove(Lock lock) throws RuntimeException{
        remove(lock.label);
    }
    public void remove(String label) throws RuntimeException{
        List<Lock> locks = getAll();
        Set<String> finLocks = new HashSet<>();
        boolean exi = false;
        for (Lock existLock : locks) {
            if(existLock.label.equals(label)){
                exi = true;
            }else{
                finLocks.add(existLock.toString());
            }
        }
        if(!exi){
            throw new RuntimeException("未找到门锁");
        }
        ssp.setVal("locks",finLocks);
    }
    public void setMac(String label,String mac){
        List<Lock> locks = getAll();
        Set<String> finLocks = new HashSet<>();
        Lock target = null;
        for (Lock existLock : locks) {
            if(existLock.label.equals(label)){
                existLock.D_Mac=mac;
                target = existLock;
            }
            finLocks.add(existLock.toString());

        }
        if(target == null){
            throw new RuntimeException("未找到门锁");
        }
        ssp.setVal("locks",finLocks);
        // ★ 唯一的改动：如果这次学到地址的正好是默认锁，把 lock_default 也一起更新，
        //   否则默认锁会一直用旧地址，快速连接永远失败、每次都退回慢速扫描
        Lock def = getDef();
        if (def != null && def.label.equals(label)) {
            setDef(target);
        }
    }

    public void setDef(Lock o) {
        if(o == null){
            ssp.setVal("lock_default","");
        }else {
            ssp.setVal("lock_default", o.toString());
        }
    }
}
