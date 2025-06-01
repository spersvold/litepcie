#
# This file is part of LitePCIe.
#
# Copyright (c) 2025 Steffen Persvold <spersvold@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

import os

from migen import *
from migen.genlib.cdc import MultiReg
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.gen import *

from litex.soc.interconnect import axi, stream
from litex.soc.interconnect.csr import *
from litex.soc.interconnect.axi import AXILiteClockDomainCrossing as AXIClockDomainCrossing

# S7PCIEHOST ----------------------------------------------------------------------------------------
class InterruptPin:
    """
    Dummy signal module that maps onto the EventManager "irq" pin, but without the CSR overhead
    """
    def __init__(self):
        self.irq = Signal()

class S7PCIEHOST(LiteXModule):
    def __init__(self, platform, pads, vadj_pgood, data_width=64, id_width=4, cd="sys",
        # PCIe hardblock parameters.
        pcie_data_width = None,
        pcie_id_width   = None,
        pcie_ecam       = None,
        pcie_mmio       = None,
        max_link_speed  = "2.5_GT/s",
        refclk_freq     = 100e6,
    ):
        # Interfaces --------------------------------------------------------------------------------
        self.axi_ctl  = axi.AXILiteInterface(data_width=32, clock_domain=cd)
        self.axi_mmio = axi.AXIInterface(data_width=data_width, id_width=id_width, clock_domain=cd)
        self.axi_dma  = axi.AXIInterface(data_width=data_width, id_width=id_width, clock_domain=cd)

        # Parameters/Locals ------------------------------------------------------------------------
        if pcie_data_width is None: pcie_data_width = data_width
        if pcie_id_width   is None: pcie_id_width   = id_width
        self.platform         = platform
        self.pcie_data_width  = pcie_data_width
        self.pcie_id_width    = pcie_id_width
        self.pcie_ecam        = pcie_ecam
        self.pcie_mmio        = pcie_mmio
        self.max_link_speed   = max_link_speed
        self.refclk_freq      = refclk_freq

        # # #

        self.nlanes = nlanes = len(pads.tx_p)

        assert nlanes          in [1, 2, 4, 8]
        assert data_width      in [64, 128]
        assert pcie_data_width in [64, 128]
        assert refclk_freq     in [100e6, 125e6, 250e6]

        # Clocking / Reset / Interrupt -------------------------------------------------------------
        self.cd_pcie        = cd_pcie        = ClockDomain()
        self.cd_pcie_ctl    = cd_pcie_ctl    = ClockDomain()
        self.pcie_refclk    = pcie_refclk    = Signal()
        self.pcie_mmcm_lock = pcie_mmcm_lock = Signal()
        self.pcie_linkup    = pcie_linkup    = Signal()

        if hasattr(pads, "rst_n"):
            self.comb += pads.rst_n.eq(~ResetSignal("pcie"))
        self.specials += Instance("IBUFDS_GTE2",
            i_CEB = 0,
            i_I   = pads.clk_p,
            i_IB  = pads.clk_n,
            o_O   = pcie_refclk
        )
        platform.add_period_constraint(pads.clk_p, 1e9/refclk_freq)

        # Reset synchronizer for PCIe domain
        self.specials += AsyncResetSynchronizer(self.cd_pcie, ResetSignal("sys") | (~pcie_mmcm_lock) | (~vadj_pgood))
        self.comb += cd_pcie_ctl.rst.eq(ResetSignal("pcie")) # Share reset

        # Add "ev" object so we can use the normal interrupt allocation
        self.ev = InterruptPin()

        # AXI Clock Domain Crossings -------------------------------------------------------------------
        axi_ctl_pcie  = axi.AXILiteInterface(data_width=32, clock_domain="pcie_ctl")
        axi_mmio_pcie = axi.AXIInterface(data_width=pcie_data_width, id_width=id_width, clock_domain="pcie")
        axi_dma_pcie  = axi.AXIInterface(data_width=pcie_data_width, id_width=0, clock_domain="pcie")

        # Hard IP ----------------------------------------------------------------------------------
        self.pcie_host_params = dict(
            # PCI Express Interface ----------------------------------------------------------------
            # RefClk
            i_REFCLK                                     = pcie_refclk,

            # TX
            o_pci_exp_txp                                = pads.tx_p,
            o_pci_exp_txn                                = pads.tx_n,

            # RX
            i_pci_exp_rxp                                = pads.rx_p,
            i_pci_exp_rxn                                = pads.rx_n,

            # Clock/Reset/Misc ---------------------------------------------------------------------
            # Common
            i_axi_aresetn                                = ~ResetSignal("pcie"),
            o_axi_aclk_out                               = ClockSignal("pcie"),
            o_axi_ctl_aclk_out                           = ClockSignal("pcie_ctl"),
            o_user_link_up                               = self.add_resync(pcie_linkup, "sys"),
            o_mmcm_lock                                  = self.add_resync(pcie_mmcm_lock, "sys"),
            o_interrupt_out                              = self.add_resync(self.ev.irq, "sys"),

            # AXI-CTL Interface --------------------------------------------------------------------
            # AW Channel
            i_s_axi_ctl_awaddr                           = axi_ctl_pcie.aw.addr,
            i_s_axi_ctl_awvalid                          = axi_ctl_pcie.aw.valid,
            o_s_axi_ctl_awready                          = axi_ctl_pcie.aw.ready,

            # W Channel
            i_s_axi_ctl_wdata                            = axi_ctl_pcie.w.data,
            i_s_axi_ctl_wstrb                            = axi_ctl_pcie.w.strb,
            i_s_axi_ctl_wvalid                           = axi_ctl_pcie.w.valid,
            o_s_axi_ctl_wready                           = axi_ctl_pcie.w.ready,

            # B Channel
            o_s_axi_ctl_bresp                            = axi_ctl_pcie.b.resp,
            o_s_axi_ctl_bvalid                           = axi_ctl_pcie.b.valid,
            i_s_axi_ctl_bready                           = axi_ctl_pcie.b.ready,

            # AR Channel
            i_s_axi_ctl_araddr                           = axi_ctl_pcie.ar.addr,
            i_s_axi_ctl_arvalid                          = axi_ctl_pcie.ar.valid,
            o_s_axi_ctl_arready                          = axi_ctl_pcie.ar.ready,

            # R Channel
            o_s_axi_ctl_rdata                            = axi_ctl_pcie.r.data,
            o_s_axi_ctl_rresp                            = axi_ctl_pcie.r.resp,
            o_s_axi_ctl_rvalid                           = axi_ctl_pcie.r.valid,
            i_s_axi_ctl_rready                           = axi_ctl_pcie.r.ready,

            # AXI-S Interface ----------------------------------------------------------------------
            # AW Channel
            i_s_axi_awid                                 = axi_mmio_pcie.aw.id,
            i_s_axi_awaddr                               = axi_mmio_pcie.aw.addr,
            i_s_axi_awlen                                = axi_mmio_pcie.aw.len,
            i_s_axi_awsize                               = axi_mmio_pcie.aw.size,
            i_s_axi_awburst                              = axi_mmio_pcie.aw.burst,
            i_s_axi_awregion                             = axi_mmio_pcie.aw.region,
            i_s_axi_awvalid                              = axi_mmio_pcie.aw.valid,
            o_s_axi_awready                              = axi_mmio_pcie.aw.ready,

            # W Channel
            i_s_axi_wdata                                = axi_mmio_pcie.w.data,
            i_s_axi_wstrb                                = axi_mmio_pcie.w.strb,
            i_s_axi_wlast                                = axi_mmio_pcie.w.last,
            i_s_axi_wvalid                               = axi_mmio_pcie.w.valid,
            o_s_axi_wready                               = axi_mmio_pcie.w.ready,

            # B Channel
            o_s_axi_bid                                  = axi_mmio_pcie.b.id,
            o_s_axi_bresp                                = axi_mmio_pcie.b.resp,
            o_s_axi_bvalid                               = axi_mmio_pcie.b.valid,
            i_s_axi_bready                               = axi_mmio_pcie.b.ready,

            # AR Channel
            i_s_axi_arid                                 = axi_mmio_pcie.ar.id,
            i_s_axi_araddr                               = axi_mmio_pcie.ar.addr,
            i_s_axi_arlen                                = axi_mmio_pcie.ar.len,
            i_s_axi_arsize                               = axi_mmio_pcie.ar.size,
            i_s_axi_arburst                              = axi_mmio_pcie.ar.burst,
            i_s_axi_arregion                             = axi_mmio_pcie.ar.region,
            i_s_axi_arvalid                              = axi_mmio_pcie.ar.valid,
            o_s_axi_arready                              = axi_mmio_pcie.ar.ready,

            # R Channel
            o_s_axi_rid                                  = axi_mmio_pcie.r.id,
            o_s_axi_rdata                                = axi_mmio_pcie.r.data,
            o_s_axi_rresp                                = axi_mmio_pcie.r.resp,
            o_s_axi_rlast                                = axi_mmio_pcie.r.last,
            o_s_axi_rvalid                               = axi_mmio_pcie.r.valid,
            i_s_axi_rready                               = axi_mmio_pcie.r.ready,

            # AXI-M Interface ----------------------------------------------------------------------
            # AW Channel
            o_m_axi_awaddr                               = axi_dma_pcie.aw.addr,
            o_m_axi_awlen                                = axi_dma_pcie.aw.len,
            o_m_axi_awsize                               = axi_dma_pcie.aw.size,
            o_m_axi_awburst                              = axi_dma_pcie.aw.burst,
            o_m_axi_awlock                               = axi_dma_pcie.aw.lock,
            o_m_axi_awcache                              = axi_dma_pcie.aw.cache,
            o_m_axi_awprot                               = axi_dma_pcie.aw.prot,
            o_m_axi_awvalid                              = axi_dma_pcie.aw.valid,
            i_m_axi_awready                              = axi_dma_pcie.aw.ready,

            # W Channel
            o_m_axi_wdata                                = axi_dma_pcie.w.data,
            o_m_axi_wstrb                                = axi_dma_pcie.w.strb,
            o_m_axi_wlast                                = axi_dma_pcie.w.last,
            o_m_axi_wvalid                               = axi_dma_pcie.w.valid,
            i_m_axi_wready                               = axi_dma_pcie.w.ready,

            # B Channel
            i_m_axi_bresp                                = axi_dma_pcie.b.resp,
            i_m_axi_bvalid                               = axi_dma_pcie.b.valid,
            o_m_axi_bready                               = axi_dma_pcie.b.ready,

            # AR Channel
            o_m_axi_araddr                               = axi_dma_pcie.ar.addr,
            o_m_axi_arlen                                = axi_dma_pcie.ar.len,
            o_m_axi_arsize                               = axi_dma_pcie.ar.size,
            o_m_axi_arburst                              = axi_dma_pcie.ar.burst,
            o_m_axi_arlock                               = axi_dma_pcie.ar.lock,
            o_m_axi_arcache                              = axi_dma_pcie.ar.cache,
            o_m_axi_arprot                               = axi_dma_pcie.ar.prot,
            o_m_axi_arvalid                              = axi_dma_pcie.ar.valid,
            i_m_axi_arready                              = axi_dma_pcie.ar.ready,

            # R Channel
            i_m_axi_rdata                                = axi_dma_pcie.r.data,
            i_m_axi_rresp                                = axi_dma_pcie.r.resp,
            i_m_axi_rlast                                = axi_dma_pcie.r.last,
            i_m_axi_rvalid                               = axi_dma_pcie.r.valid,
            o_m_axi_rready                               = axi_dma_pcie.r.ready,

            # MSI interface ------------------------------------------------------------------------
            # FIXME!
            i_INTX_MSI_Request                           = 0,
            o_INTX_MSI_Grant                             = Open(),
            o_MSI_enable                                 = Open(),
            i_MSI_Vector_Num                             = 0,
            o_MSI_Vector_Width                           = Open(),
        )

        self.pcie_conv_ctl_params = dict(
            # AXI-S Interface ----------------------------------------------------------------------
            # Common
            i_s_axi_aclk                                 = ClockSignal("sys"),
            i_s_axi_aresetn                              = ~ResetSignal("sys"),

            # AW Channel
            i_s_axi_awaddr                               = self.axi_ctl.aw.addr,
            i_s_axi_awprot                               = self.axi_ctl.aw.prot,
            i_s_axi_awvalid                              = self.axi_ctl.aw.valid,
            o_s_axi_awready                              = self.axi_ctl.aw.ready,

            # W Channel
            i_s_axi_wdata                                = self.axi_ctl.w.data,
            i_s_axi_wstrb                                = self.axi_ctl.w.strb,
            i_s_axi_wvalid                               = self.axi_ctl.w.valid,
            o_s_axi_wready                               = self.axi_ctl.w.ready,

            # B Channel
            o_s_axi_bresp                                = self.axi_ctl.b.resp,
            o_s_axi_bvalid                               = self.axi_ctl.b.valid,
            i_s_axi_bready                               = self.axi_ctl.b.ready,

            # AR Channel
            i_s_axi_araddr                               = self.axi_ctl.ar.addr,
            i_s_axi_arprot                               = self.axi_ctl.ar.prot,
            i_s_axi_arvalid                              = self.axi_ctl.ar.valid,
            o_s_axi_arready                              = self.axi_ctl.ar.ready,

            # R Channel
            o_s_axi_rdata                                = self.axi_ctl.r.data,
            o_s_axi_rresp                                = self.axi_ctl.r.resp,
            o_s_axi_rvalid                               = self.axi_ctl.r.valid,
            i_s_axi_rready                               = self.axi_ctl.r.ready,

            # AXI-M Interface ----------------------------------------------------------------------
            # Common
            i_m_axi_aclk                                 = ClockSignal("pcie_ctl"),
            i_m_axi_aresetn                              = ~ResetSignal("pcie"),

            # AW Channel
            o_m_axi_awaddr                               = axi_ctl_pcie.aw.addr,
            o_m_axi_awprot                               = axi_ctl_pcie.aw.prot,
            o_m_axi_awvalid                              = axi_ctl_pcie.aw.valid,
            i_m_axi_awready                              = axi_ctl_pcie.aw.ready,

            # AW Channel
            o_m_axi_wdata                                = axi_ctl_pcie.w.data,
            o_m_axi_wstrb                                = axi_ctl_pcie.w.strb,
            o_m_axi_wvalid                               = axi_ctl_pcie.w.valid,
            i_m_axi_wready                               = axi_ctl_pcie.w.ready,

            # B Channel
            i_m_axi_bresp                                = axi_ctl_pcie.b.resp,
            i_m_axi_bvalid                               = axi_ctl_pcie.b.valid,
            o_m_axi_bready                               = axi_ctl_pcie.b.ready,

            # AR Channel
            o_m_axi_araddr                               = axi_ctl_pcie.ar.addr,
            o_m_axi_arprot                               = axi_ctl_pcie.ar.prot,
            o_m_axi_arvalid                              = axi_ctl_pcie.ar.valid,
            i_m_axi_arready                              = axi_ctl_pcie.ar.ready,

            # R Channel
            i_m_axi_rdata                                = axi_ctl_pcie.r.data,
            i_m_axi_rresp                                = axi_ctl_pcie.r.resp,
            i_m_axi_rvalid                               = axi_ctl_pcie.r.valid,
            o_m_axi_rready                               = axi_ctl_pcie.r.ready,
        )

        self.pcie_conv_mmio_params = dict(
            # AXI-S Interface ----------------------------------------------------------------------
            # Common
            i_s_axi_aclk                                 = ClockSignal("sys"),
            i_s_axi_aresetn                              = ~ResetSignal("sys"),

            # AW Channel
            i_s_axi_awid                                 = self.axi_mmio.aw.id,
            i_s_axi_awaddr                               = self.axi_mmio.aw.addr,
            i_s_axi_awlen                                = self.axi_mmio.aw.len,
            i_s_axi_awsize                               = self.axi_mmio.aw.size,
            i_s_axi_awburst                              = self.axi_mmio.aw.burst,
            i_s_axi_awlock                               = self.axi_mmio.aw.lock,
            i_s_axi_awcache                              = self.axi_mmio.aw.cache,
            i_s_axi_awprot                               = self.axi_mmio.aw.prot,
            i_s_axi_awregion                             = self.axi_mmio.aw.region,
            i_s_axi_awqos                                = self.axi_mmio.aw.qos,
            i_s_axi_awvalid                              = self.axi_mmio.aw.valid,
            o_s_axi_awready                              = self.axi_mmio.aw.ready,

            # W Channel
            i_s_axi_wdata                                = self.axi_mmio.w.data,
            i_s_axi_wstrb                                = self.axi_mmio.w.strb,
            i_s_axi_wlast                                = self.axi_mmio.w.last,
            i_s_axi_wvalid                               = self.axi_mmio.w.valid,
            o_s_axi_wready                               = self.axi_mmio.w.ready,

            # B Channel
            o_s_axi_bid                                  = self.axi_mmio.b.id,
            o_s_axi_bresp                                = self.axi_mmio.b.resp,
            o_s_axi_bvalid                               = self.axi_mmio.b.valid,
            i_s_axi_bready                               = self.axi_mmio.b.ready,

            # AR Channel
            i_s_axi_arid                                 = self.axi_mmio.ar.id,
            i_s_axi_araddr                               = self.axi_mmio.ar.addr,
            i_s_axi_arlen                                = self.axi_mmio.ar.len,
            i_s_axi_arsize                               = self.axi_mmio.ar.size,
            i_s_axi_arburst                              = self.axi_mmio.ar.burst,
            i_s_axi_arlock                               = self.axi_mmio.ar.lock,
            i_s_axi_arcache                              = self.axi_mmio.ar.cache,
            i_s_axi_arprot                               = self.axi_mmio.ar.prot,
            i_s_axi_arregion                             = self.axi_mmio.ar.region,
            i_s_axi_arqos                                = self.axi_mmio.ar.qos,
            i_s_axi_arvalid                              = self.axi_mmio.ar.valid,
            o_s_axi_arready                              = self.axi_mmio.ar.ready,

            # R Channel
            o_s_axi_rid                                  = self.axi_mmio.r.id,
            o_s_axi_rdata                                = self.axi_mmio.r.data,
            o_s_axi_rresp                                = self.axi_mmio.r.resp,
            o_s_axi_rlast                                = self.axi_mmio.r.last,
            o_s_axi_rvalid                               = self.axi_mmio.r.valid,
            i_s_axi_rready                               = self.axi_mmio.r.ready,

            # AXI-M Interface ----------------------------------------------------------------------
            # Common
            i_m_axi_aclk                                 = ClockSignal("pcie"),
            i_m_axi_aresetn                              = ~ResetSignal("pcie"),

            # AW Channel
            o_m_axi_awid                                 = axi_mmio_pcie.aw.id,
            o_m_axi_awaddr                               = axi_mmio_pcie.aw.addr,
            o_m_axi_awlen                                = axi_mmio_pcie.aw.len,
            o_m_axi_awsize                               = axi_mmio_pcie.aw.size,
            o_m_axi_awburst                              = axi_mmio_pcie.aw.burst,
            o_m_axi_awlock                               = axi_mmio_pcie.aw.lock,
            o_m_axi_awcache                              = axi_mmio_pcie.aw.cache,
            o_m_axi_awprot                               = axi_mmio_pcie.aw.prot,
            o_m_axi_awregion                             = axi_mmio_pcie.aw.region,
            o_m_axi_awqos                                = axi_mmio_pcie.aw.qos,
            o_m_axi_awvalid                              = axi_mmio_pcie.aw.valid,
            i_m_axi_awready                              = axi_mmio_pcie.aw.ready,

            # W Channel
            o_m_axi_wdata                                = axi_mmio_pcie.w.data,
            o_m_axi_wstrb                                = axi_mmio_pcie.w.strb,
            o_m_axi_wlast                                = axi_mmio_pcie.w.last,
            o_m_axi_wvalid                               = axi_mmio_pcie.w.valid,
            i_m_axi_wready                               = axi_mmio_pcie.w.ready,

            # B Channel
            i_m_axi_bid                                  = axi_mmio_pcie.b.id,
            i_m_axi_bresp                                = axi_mmio_pcie.b.resp,
            i_m_axi_bvalid                               = axi_mmio_pcie.b.valid,
            o_m_axi_bready                               = axi_mmio_pcie.b.ready,

            # AR Channel
            o_m_axi_arid                                 = axi_mmio_pcie.ar.id,
            o_m_axi_araddr                               = axi_mmio_pcie.ar.addr,
            o_m_axi_arlen                                = axi_mmio_pcie.ar.len,
            o_m_axi_arsize                               = axi_mmio_pcie.ar.size,
            o_m_axi_arburst                              = axi_mmio_pcie.ar.burst,
            o_m_axi_arlock                               = axi_mmio_pcie.ar.lock,
            o_m_axi_arcache                              = axi_mmio_pcie.ar.cache,
            o_m_axi_arprot                               = axi_mmio_pcie.ar.prot,
            o_m_axi_arregion                             = axi_mmio_pcie.ar.region,
            o_m_axi_arqos                                = axi_mmio_pcie.ar.qos,
            o_m_axi_arvalid                              = axi_mmio_pcie.ar.valid,
            i_m_axi_arready                              = axi_mmio_pcie.ar.ready,

            # R Channel
            i_m_axi_rid                                  = axi_mmio_pcie.r.id,
            i_m_axi_rdata                                = axi_mmio_pcie.r.data,
            i_m_axi_rresp                                = axi_mmio_pcie.r.resp,
            i_m_axi_rlast                                = axi_mmio_pcie.r.last,
            i_m_axi_rvalid                               = axi_mmio_pcie.r.valid,
            o_m_axi_rready                               = axi_mmio_pcie.r.ready,
        )

        self.pcie_conv_dma_params = dict(
            # AXI-S Interface ----------------------------------------------------------------------
            # Common
            i_s_axi_aclk                                 = ClockSignal("pcie"),
            i_s_axi_aresetn                              = ~ResetSignal("pcie"),

            # AW Channel
            i_s_axi_awid                                 = axi_dma_pcie.aw.id,
            i_s_axi_awaddr                               = axi_dma_pcie.aw.addr,
            i_s_axi_awlen                                = axi_dma_pcie.aw.len,
            i_s_axi_awsize                               = axi_dma_pcie.aw.size,
            i_s_axi_awburst                              = axi_dma_pcie.aw.burst,
            i_s_axi_awlock                               = axi_dma_pcie.aw.lock,
            i_s_axi_awcache                              = axi_dma_pcie.aw.cache,
            i_s_axi_awprot                               = axi_dma_pcie.aw.prot,
            i_s_axi_awregion                             = axi_dma_pcie.aw.region,
            i_s_axi_awqos                                = axi_dma_pcie.aw.qos,
            i_s_axi_awvalid                              = axi_dma_pcie.aw.valid,
            o_s_axi_awready                              = axi_dma_pcie.aw.ready,

            # W Channel
            i_s_axi_wdata                                = axi_dma_pcie.w.data,
            i_s_axi_wstrb                                = axi_dma_pcie.w.strb,
            i_s_axi_wlast                                = axi_dma_pcie.w.last,
            i_s_axi_wvalid                               = axi_dma_pcie.w.valid,
            o_s_axi_wready                               = axi_dma_pcie.w.ready,

            # B Channel
            o_s_axi_bid                                  = axi_dma_pcie.b.id,
            o_s_axi_bresp                                = axi_dma_pcie.b.resp,
            o_s_axi_bvalid                               = axi_dma_pcie.b.valid,
            i_s_axi_bready                               = axi_dma_pcie.b.ready,

            # AR Channel
            i_s_axi_arid                                 = axi_dma_pcie.ar.id,
            i_s_axi_araddr                               = axi_dma_pcie.ar.addr,
            i_s_axi_arlen                                = axi_dma_pcie.ar.len,
            i_s_axi_arsize                               = axi_dma_pcie.ar.size,
            i_s_axi_arburst                              = axi_dma_pcie.ar.burst,
            i_s_axi_arlock                               = axi_dma_pcie.ar.lock,
            i_s_axi_arcache                              = axi_dma_pcie.ar.cache,
            i_s_axi_arprot                               = axi_dma_pcie.ar.prot,
            i_s_axi_arregion                             = axi_dma_pcie.ar.region,
            i_s_axi_arqos                                = axi_dma_pcie.ar.qos,
            i_s_axi_arvalid                              = axi_dma_pcie.ar.valid,
            o_s_axi_arready                              = axi_dma_pcie.ar.ready,

            # R Channel
            o_s_axi_rid                                  = axi_dma_pcie.r.id,
            o_s_axi_rdata                                = axi_dma_pcie.r.data,
            o_s_axi_rresp                                = axi_dma_pcie.r.resp,
            o_s_axi_rlast                                = axi_dma_pcie.r.last,
            o_s_axi_rvalid                               = axi_dma_pcie.r.valid,
            i_s_axi_rready                               = axi_dma_pcie.r.ready,

            # AXI-M Interface ----------------------------------------------------------------------
            # Common
            i_m_axi_aclk                                 = ClockSignal("sys"),
            i_m_axi_aresetn                              = ~ResetSignal("sys"),

            # AW Channel
            o_m_axi_awid                                 = self.axi_dma.aw.id,
            o_m_axi_awaddr                               = self.axi_dma.aw.addr,
            o_m_axi_awlen                                = self.axi_dma.aw.len,
            o_m_axi_awsize                               = self.axi_dma.aw.size,
            o_m_axi_awburst                              = self.axi_dma.aw.burst,
            o_m_axi_awlock                               = self.axi_dma.aw.lock,
            o_m_axi_awcache                              = self.axi_dma.aw.cache,
            o_m_axi_awprot                               = self.axi_dma.aw.prot,
            o_m_axi_awregion                             = self.axi_dma.aw.region,
            o_m_axi_awqos                                = self.axi_dma.aw.qos,
            o_m_axi_awvalid                              = self.axi_dma.aw.valid,
            i_m_axi_awready                              = self.axi_dma.aw.ready,

            # W Channel
            o_m_axi_wdata                                = self.axi_dma.w.data,
            o_m_axi_wstrb                                = self.axi_dma.w.strb,
            o_m_axi_wlast                                = self.axi_dma.w.last,
            o_m_axi_wvalid                               = self.axi_dma.w.valid,
            i_m_axi_wready                               = self.axi_dma.w.ready,

            # B Channel
            i_m_axi_bid                                  = self.axi_dma.b.id,
            i_m_axi_bresp                                = self.axi_dma.b.resp,
            i_m_axi_bvalid                               = self.axi_dma.b.valid,
            o_m_axi_bready                               = self.axi_dma.b.ready,

            # AR Channel
            o_m_axi_arid                                 = self.axi_dma.ar.id,
            o_m_axi_araddr                               = self.axi_dma.ar.addr,
            o_m_axi_arlen                                = self.axi_dma.ar.len,
            o_m_axi_arsize                               = self.axi_dma.ar.size,
            o_m_axi_arburst                              = self.axi_dma.ar.burst,
            o_m_axi_arlock                               = self.axi_dma.ar.lock,
            o_m_axi_arcache                              = self.axi_dma.ar.cache,
            o_m_axi_arprot                               = self.axi_dma.ar.prot,
            o_m_axi_arregion                             = self.axi_dma.ar.region,
            o_m_axi_arqos                                = self.axi_dma.ar.qos,
            o_m_axi_arvalid                              = self.axi_dma.ar.valid,
            i_m_axi_arready                              = self.axi_dma.ar.ready,

            # R Channel
            i_m_axi_rid                                  = self.axi_dma.r.id,
            i_m_axi_rdata                                = self.axi_dma.r.data,
            i_m_axi_rresp                                = self.axi_dma.r.resp,
            i_m_axi_rlast                                = self.axi_dma.r.last,
            i_m_axi_rvalid                               = self.axi_dma.r.valid,
            o_m_axi_rready                               = self.axi_dma.r.ready,
        )

    # Resync Helper --------------------------------------------------------------------------------
    def add_resync(self, sig, clk="sys"):
        _sig = Signal.like(sig)
        self.specials += MultiReg(_sig, sig, clk)
        return _sig

    # Region Helper --------------------------------------------------------------------------------
    def update_regions(self, ecam, mmio):
        print(f"ECAM REGION {ecam}")
        print(f"MMIO REGION {mmio}")

        self.pcie_ecam  = ecam
        self.pcie_mmio  = mmio

    # Hard IP sources ------------------------------------------------------------------------------
    def add_sources(self, platform):

        assert self.pcie_ecam, "No ECAM region provided"
        assert self.pcie_mmio, "No MMIO region provided"

        # Global parameters.
        configs = {
            "conv_ctl" : {
                "ip_type" : "axi_clock_converter",
                "config"  : {
                    "ID_WIDTH"                    : 0,
                    "DATA_WIDTH"                  : 32,
                    "PROTOCOL"                    : "AXI4LITE",
                    "SYNCHRONIZATION_STAGES"      : 2,
                },
            },
            "conv_mmio" : {
                "ip_type" : "axi_clock_converter",
                "config"  : {
                    "ID_WIDTH"                    : f"{self.pcie_id_width}",
                    "DATA_WIDTH"                  : f"{self.pcie_data_width}",
                    "PROTOCOL"                    : "AXI4",
                    "SYNCHRONIZATION_STAGES"      : 2,
                },
            },
            "conv_dma" : {
                "ip_type" : "axi_clock_converter",
                "config"  : {
                    "ID_WIDTH"                    : f"{self.pcie_id_width}",
                    "DATA_WIDTH"                  : f"{self.pcie_data_width}",
                    "PROTOCOL"                    : "AXI4",
                    "SYNCHRONIZATION_STAGES"      : 2,
                },
            },
            "host" : {
                "ip_type" : "axi_pcie",
                "config"  : {
                    "AXIBAR2PCIEBAR_0"            : f"0x{self.pcie_mmio.origin:08x}",
                    "AXIBAR_0"                    : f"0x{self.pcie_mmio.origin:08x}",
                    "AXIBAR_HIGHADDR_0"           : f"0x{self.pcie_mmio.origin+self.pcie_mmio.size-1:08x}",
                    "AXIBAR_NUM"                  : 1,
                    "BAR0_SCALE"                  : "Gigabytes",
                    "BAR0_SIZE"                   : 4,
                    "BAR_64BIT"                   : "true",
                    "BASEADDR"                    : f"0x{self.pcie_ecam.origin:08x}",
                    "HIGHADDR"                    : f"0x{self.pcie_ecam.origin+self.pcie_ecam.size-1:08x}",
                    "CLASS_CODE"                  : "0x060400",
                    "ENABLE_CLASS_CODE"           : "true",
                    "BASE_CLASS_MENU"             : "Bridge_device",
                    "SUB_CLASS_INTERFACE_MENU"    : "PCI_to_PCI_bridge",
                    "NO_OF_LANES"                 : f"X{self.nlanes}",
                    "MAX_LINK_SPEED"              : f"{self.max_link_speed}",
                    "REF_CLK_FREQ"                : f"{int(self.refclk_freq/1e6)}_MHz",
                    "DEVICE_ID"                   : "0x7111",
                    "INCLUDE_BAROFFSET_REG"       : "false",
                    "INCLUDE_RC"                  : "Root_Port_of_PCI_Express_Root_Complex",
                    "S_AXI_SUPPORTS_NARROW_BURST" : "true",
                    "S_AXI_ID_WIDTH"              : f"{self.pcie_id_width}",
                    "S_AXI_DATA_WIDTH"            : f"{self.pcie_data_width}",
                    "M_AXI_DATA_WIDTH"            : f"{self.pcie_data_width}",
                    "COMP_TIMEOUT"                : "50ms",
                    "rp_bar_hide"                 : "true",
                },
            },
        }

        # Tcl generation.
        ip_tcl = []
        for name, config in configs.items():
            ip_type = config["ip_type"]
            module_name = f"pcie_{name}_s7"
            ip_tcl.append(f"create_ip -vendor xilinx.com -name {ip_type} -module_name {module_name}")
            ip_tcl.append(f"set obj [get_ips {module_name}]")
            ip_tcl.append("set_property -dict [list \\")
            for config, value in config["config"].items():
                ip_tcl.append("CONFIG.{} {} \\".format(config, '{{' + str(value) + '}}'))
            ip_tcl.append("] $obj")
            ip_tcl.append("generate_target all $obj")
            ip_tcl.append("synth_ip $obj")
            ip_tcl.append("")

        platform.toolchain.pre_synthesis_commands += ip_tcl


    # Finalize -------------------------------------------------------------------------------------
    def do_finalize(self):
        self.add_sources(self.platform)
        self.specials += [
            Instance("pcie_conv_ctl_s7", **self.pcie_conv_ctl_params),
            Instance("pcie_conv_mmio_s7", **self.pcie_conv_mmio_params),
            Instance("pcie_conv_dma_s7", **self.pcie_conv_dma_params),
            Instance("pcie_host_s7", **self.pcie_host_params),
        ]
