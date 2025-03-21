
/*
 * Copyright Contributors to the OpenCue Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
 * in compliance with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software distributed under the License
 * is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
 * or implied. See the License for the specific language governing permissions and limitations under
 * the License.
 */

package com.imageworks.spcue.test.dao.postgres;

import java.sql.Timestamp;
import java.util.Map;
import javax.annotation.Resource;

import com.google.common.collect.ImmutableList;
import org.junit.Rule;
import org.junit.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.annotation.Rollback;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.test.context.junit4.AbstractTransactionalJUnit4SpringContextTests;
import org.springframework.test.context.support.AnnotationConfigContextLoader;
import org.springframework.test.context.transaction.AfterTransaction;
import org.springframework.test.context.transaction.BeforeTransaction;
import org.springframework.transaction.annotation.Transactional;

import com.imageworks.spcue.DispatchHost;
import com.imageworks.spcue.HostEntity;
import com.imageworks.spcue.HostInterface;
import com.imageworks.spcue.Source;
import com.imageworks.spcue.config.TestAppConfig;
import com.imageworks.spcue.dao.AllocationDao;
import com.imageworks.spcue.dao.FacilityDao;
import com.imageworks.spcue.dao.HostDao;
import com.imageworks.spcue.grpc.host.HardwareState;
import com.imageworks.spcue.grpc.host.HostTagType;
import com.imageworks.spcue.grpc.host.LockState;
import com.imageworks.spcue.grpc.host.ThreadMode;
import com.imageworks.spcue.grpc.report.HostReport;
import com.imageworks.spcue.grpc.report.RenderHost;
import com.imageworks.spcue.service.HostManager;
import com.imageworks.spcue.test.AssumingPostgresEngine;
import com.imageworks.spcue.util.CueUtil;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

@Transactional
@ContextConfiguration(classes = TestAppConfig.class, loader = AnnotationConfigContextLoader.class)
public class HostDaoTests extends AbstractTransactionalJUnit4SpringContextTests {

    @Autowired
    @Rule
    public AssumingPostgresEngine assumingPostgresEngine;

    private static final String TEST_HOST = "beta";

    @Resource
    protected AllocationDao allocationDao;

    @Resource
    protected HostDao hostDao;

    @Resource
    protected HostManager hostManager;

    @Resource
    protected FacilityDao facilityDao;

    public HostDaoTests() {}

    // Hardcoded value of dispatcher.memory.mem_reserved_system
    // to avoid having to read opencue.properties on a test setting
    private final long MEM_RESERVED_SYSTEM = 524288;

    public static RenderHost buildRenderHost(String name) {
        RenderHost host = RenderHost.newBuilder().setName(name).setBootTime(1192369572)
                // The minimum amount of free space in the temporary directory to book a host.
                .setFreeMcp(CueUtil.GB).setFreeMem(15290520).setFreeSwap((int) CueUtil.MB512)
                .setLoad(1).setNimbyEnabled(false).setTotalMcp(CueUtil.GB4)
                .setTotalMem((int) CueUtil.GB16).setTotalSwap((int) CueUtil.GB2)
                .setNimbyEnabled(false).setNumProcs(2).setCoresPerProc(400)
                .addAllTags(ImmutableList.of("linux", "64bit")).setState(HardwareState.UP)
                .setFacility("spi").setFreeGpuMem((int) CueUtil.MB512)
                .setTotalGpuMem((int) CueUtil.MB512).build();

        return host;
    }

    @Test
    public void testInit() {}

    @BeforeTransaction
    public void clear() {
        jdbcTemplate.update("DELETE FROM host WHERE str_name=?", TEST_HOST);
    }

    @AfterTransaction
    public void destroy() {
        jdbcTemplate.update("DELETE FROM host WHERE str_name=?", TEST_HOST);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHost() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        assertEquals(Long.valueOf(CueUtil.GB16 - this.MEM_RESERVED_SYSTEM),
                jdbcTemplate.queryForObject("SELECT int_mem FROM host WHERE str_name=?", Long.class,
                        TEST_HOST));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostFQDN1() {
        String TEST_HOST_NEW = "ice-ns1.yvr";
        String FQDN_HOST = TEST_HOST_NEW + ".spimageworks.com";
        hostDao.insertRenderHost(buildRenderHost(FQDN_HOST),
                hostManager.getDefaultAllocationDetail(), true);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);

        HostInterface host = hostDao.findHost(FQDN_HOST);
        HostEntity hostDetail2 = hostDao.getHostDetail(host);
        assertEquals(TEST_HOST_NEW, hostDetail2.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostFQDN2() {
        String TEST_HOST_NEW = "compile21";
        String FQDN_HOST = TEST_HOST_NEW + ".spimageworks.com";
        hostDao.insertRenderHost(buildRenderHost(FQDN_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);

        HostInterface host = hostDao.findHost(FQDN_HOST);
        HostEntity hostDetail2 = hostDao.getHostDetail(host);
        assertEquals(TEST_HOST_NEW, hostDetail2.name);

    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostFQDN3() {
        String TEST_HOST_NEW = "hostname";
        String FQDN_HOST = TEST_HOST_NEW + ".fake.co.uk";
        hostDao.insertRenderHost(buildRenderHost(FQDN_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);

        HostInterface host = hostDao.findHost(FQDN_HOST);
        HostEntity hostDetail2 = hostDao.getHostDetail(host);
        assertEquals(TEST_HOST_NEW, hostDetail2.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostFQDN4() {
        String TEST_HOST_NEW = "10.0.1.18";
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST_NEW),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostIPv61() {
        String TEST_HOST_NEW = "::1";
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST_NEW),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostIPv62() {
        String TEST_HOST_NEW = "ABCD:ABCD:ABCD:ABCD:ABCD:ABCD:ABCD:ABCD";
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST_NEW),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostIPv63() {
        String TEST_HOST_NEW = "ABCD:ABCD:ABCD:ABCD:ABCD:ABCD:192.168.100.180";
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST_NEW),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST_NEW);
        assertEquals(TEST_HOST_NEW, hostDetail.name);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostAlternateOS() {

        RenderHost host =
                buildRenderHost(TEST_HOST).toBuilder().putAttributes("SP_OS", "spinux1").build();

        hostDao.insertRenderHost(host, hostManager.getDefaultAllocationDetail(), false);

        assertEquals("spinux1",
                jdbcTemplate.queryForObject(
                        "SELECT str_os FROM host_stat, host "
                                + "WHERE host.pk_host = host_stat.pk_host " + "AND host.str_name=?",
                        String.class, TEST_HOST),
                "spinux1");
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testInsertHostDesktop() {

        RenderHost host = buildRenderHost(TEST_HOST);
        hostDao.insertRenderHost(host, hostManager.getDefaultAllocationDetail(), false);

        assertEquals(Long.valueOf(CueUtil.GB16 - this.MEM_RESERVED_SYSTEM),
                jdbcTemplate.queryForObject("SELECT int_mem FROM host WHERE str_name=?", Long.class,
                        TEST_HOST));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testUpdateThreadMode() {

        RenderHost host = buildRenderHost(TEST_HOST);
        host.toBuilder().setNimbyEnabled(true).build();
        hostDao.insertRenderHost(host, hostManager.getDefaultAllocationDetail(), false);

        HostEntity d = hostDao.findHostDetail(TEST_HOST);
        hostDao.updateThreadMode(d, ThreadMode.AUTO);

        assertEquals(Integer.valueOf(ThreadMode.AUTO_VALUE), jdbcTemplate.queryForObject(
                "SELECT int_thread_mode FROM host WHERE pk_host=?", Integer.class, d.id));

        hostDao.updateThreadMode(d, ThreadMode.ALL);

        assertEquals(Integer.valueOf(ThreadMode.ALL_VALUE), jdbcTemplate.queryForObject(
                "SELECT int_thread_mode FROM host WHERE pk_host=?", Integer.class, d.id));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testGetHostDetail() {

        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity host = hostDao.findHostDetail(TEST_HOST);
        hostDao.getHostDetail(host);
        hostDao.getHostDetail(host.getHostId());
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testIsHostLocked() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity host = hostDao.findHostDetail(TEST_HOST);
        assertEquals(hostDao.isHostLocked(host), false);

        hostDao.updateHostLock(host, LockState.LOCKED, new Source("TEST"));
        assertEquals(hostDao.isHostLocked(host), true);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testIsHostUp() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        assertTrue(hostDao.isHostUp(hostDao.findHostDetail(TEST_HOST)));

        hostDao.updateHostState(hostDao.findHostDetail(TEST_HOST), HardwareState.DOWN);
        assertFalse(hostDao.isHostUp(hostDao.findHostDetail(TEST_HOST)));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testHostExists() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        assertEquals(hostDao.hostExists(TEST_HOST), true);
        assertEquals(hostDao.hostExists("frickjack"), false);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testDeleteHost() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity host = hostDao.findHostDetail(TEST_HOST);
        assertEquals(hostDao.hostExists(TEST_HOST), true);
        hostDao.deleteHost(host);
        assertEquals(hostDao.hostExists(TEST_HOST), false);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testDeleteDownHosts() {
        for (int i = 0; i < 3; i++) {
            String name = TEST_HOST + i;
            hostDao.insertRenderHost(buildRenderHost(name),
                    hostManager.getDefaultAllocationDetail(), false);
            if (i != 1) {
                HostEntity host = hostDao.findHostDetail(name);
                assertEquals(name, host.name);
                hostDao.updateHostState(host, HardwareState.DOWN);
            }
        }

        hostDao.deleteDownHosts();

        for (int i = 0; i < 3; i++) {
            String name = TEST_HOST + i;
            assertEquals(hostDao.hostExists(name), i == 1);
        }
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testUpdateHostRebootWhenIdle() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity host = hostDao.findHostDetail(TEST_HOST);
        assertFalse(jdbcTemplate.queryForObject("SELECT b_reboot_idle FROM host WHERE pk_host=?",
                Boolean.class, host.getHostId()));
        hostDao.updateHostRebootWhenIdle(host, true);
        assertTrue(jdbcTemplate.queryForObject("SELECT b_reboot_idle FROM host WHERE pk_host=?",
                Boolean.class, host.getHostId()));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void updateHostStats() {

        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        DispatchHost dispatchHost = hostDao.findDispatchHost(TEST_HOST);
        hostDao.updateHostStats(dispatchHost, CueUtil.GB8, CueUtil.GB8, CueUtil.GB8, CueUtil.GB8,
                CueUtil.GB8, CueUtil.GB8, 1, 1, 100, new Timestamp(1247526000 * 1000l), "spinux1");

        Map<String, Object> result = jdbcTemplate
                .queryForMap("SELECT * FROM host_stat WHERE pk_host=?", dispatchHost.getHostId());

        assertEquals(CueUtil.GB8, ((Long) (result.get("int_mem_total"))).longValue());
        assertEquals(CueUtil.GB8, ((Long) (result.get("int_mem_free"))).longValue());
        assertEquals(CueUtil.GB8, ((Long) (result.get("int_swap_total"))).longValue());
        assertEquals(CueUtil.GB8, ((Long) (result.get("int_swap_free"))).longValue());
        assertEquals(CueUtil.GB8, ((Long) (result.get("int_mcp_total"))).longValue());
        assertEquals(CueUtil.GB8, ((Long) (result.get("int_mcp_free"))).longValue());
        assertEquals(100, ((Long) (result.get("int_load"))).intValue());
        assertEquals(new Timestamp(1247526000 * 1000l), (Timestamp) result.get("ts_booted"));

    }

    @Test
    @Transactional
    @Rollback(true)
    public void updateHostResources() {

        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        DispatchHost dispatchHost = hostDao.findDispatchHost(TEST_HOST);
        HostReport report = HostReport.newBuilder().setHost(buildRenderHost(TEST_HOST).toBuilder()
                .setCoresPerProc(1200).setNumProcs(2).setTotalMem((int) CueUtil.GB32)).build();
        hostDao.updateHostResources(dispatchHost, report);

        // Verify what the original values are
        assertEquals(800, dispatchHost.cores);
        assertEquals(800, dispatchHost.idleCores);
        assertEquals(CueUtil.GB16 - this.MEM_RESERVED_SYSTEM, dispatchHost.idleMemory);
        assertEquals(CueUtil.GB16 - this.MEM_RESERVED_SYSTEM, dispatchHost.memory);

        dispatchHost = hostDao.findDispatchHost(TEST_HOST);

        // Now verify they've changed.
        assertEquals(2400, dispatchHost.cores);
        assertEquals(2400, dispatchHost.idleCores);
        assertEquals(CueUtil.GB32 - this.MEM_RESERVED_SYSTEM, dispatchHost.idleMemory);
        assertEquals(CueUtil.GB32 - this.MEM_RESERVED_SYSTEM, dispatchHost.memory);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testGetDispatchHost() {
        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST);
        DispatchHost dispatchHost = hostDao.findDispatchHost(TEST_HOST);

        assertEquals(dispatchHost.name, TEST_HOST);
        assertEquals(dispatchHost.allocationId, hostDetail.getAllocationId());
        assertEquals(dispatchHost.id, hostDetail.getHostId());
        assertEquals(dispatchHost.cores, hostDetail.cores);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testUpdateHostSetAllocation() {

        hostDao.insertRenderHost(buildRenderHost(TEST_HOST),
                hostManager.getDefaultAllocationDetail(), false);

        HostEntity hostDetail = hostDao.findHostDetail(TEST_HOST);

        hostDao.updateHostSetAllocation(hostDetail, hostManager.getDefaultAllocationDetail());

        hostDetail = hostDao.findHostDetail(TEST_HOST);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testUpdateHostSetManualTags() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));

        hostDao.tagHost(host, "frick", HostTagType.MANUAL);
        hostDao.tagHost(host, "jack", HostTagType.MANUAL);
        hostDao.recalcuateTags(host.id);

        String tag = jdbcTemplate.queryForObject("SELECT str_tags FROM host WHERE pk_host=?",
                String.class, host.id);
        assertEquals("unassigned beta 64bit frick jack linux", tag);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testUpdateHostSetOS() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));
        hostDao.updateHostOs(host, "foo");
        String tag = jdbcTemplate.queryForObject("SELECT str_os FROM host_stat WHERE pk_host=?",
                String.class, host.id);
        assertEquals("foo", tag);
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testChangeTags() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));

        String tag = jdbcTemplate.queryForObject("SELECT str_tags FROM host WHERE pk_host=?",
                String.class, host.id);
        assertEquals("unassigned beta 64bit linux", tag);

        hostDao.removeTag(host, "linux");
        hostDao.recalcuateTags(host.id);

        assertEquals("unassigned beta 64bit", jdbcTemplate.queryForObject(
                "SELECT str_tags FROM host WHERE pk_host=?", String.class, host.id));

        hostDao.tagHost(host, "32bit", HostTagType.MANUAL);
        hostDao.recalcuateTags(host.id);

        assertEquals("unassigned beta 32bit 64bit", jdbcTemplate.queryForObject(
                "SELECT str_tags FROM host WHERE pk_host=?", String.class, host.id));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testGetStrandedCoreUnits() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));

        jdbcTemplate.update("UPDATE host SET int_mem_idle = ? WHERE pk_host = ?", CueUtil.GB,
                host.getHostId());

        assertEquals(host.idleCores, hostDao.getStrandedCoreUnits(host));

        jdbcTemplate.update("UPDATE host SET int_mem_idle = ? WHERE pk_host = ?", CueUtil.GB2,
                host.getHostId());

        assertEquals(0, hostDao.getStrandedCoreUnits(host));

        // Check to see if fractional cores is rounded to the lowest
        // whole core properly.
        jdbcTemplate.update(
                "UPDATE host SET int_cores_idle=150, int_mem_idle = ? WHERE pk_host = ?",
                CueUtil.GB, host.getHostId());

        assertEquals(100, hostDao.getStrandedCoreUnits(host));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testIsPreferShow() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));
        assertFalse(hostDao.isPreferShow(host));
    }

    @Test
    @Transactional
    @Rollback(true)
    public void testIsNimby() {
        DispatchHost host = hostManager.createHost(buildRenderHost(TEST_HOST));
        assertFalse(hostDao.isNimbyHost(host));
    }
}
