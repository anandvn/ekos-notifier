// indi_power_simulator: a power box on libindi's standard INDI::PowerInterface, for testing
// Ekos Notifier's power support without hardware (libindi has no power box simulator).
//
// Four DC outputs (Camera 1.2 A, Mount 0.45 A, Focuser 0.8 A, USB hub 0.3 A when on), two dew
// outputs, two USB ports. SIMULATOR_SETTINGS: SIM_VOLTAGE (input volts) and SIM_TRIP (switch an
// output off, 1-4, as a tripped fuse would).
//
// Build (needs libindi's headers, e.g. Arch `libindi`, Debian `libindi-dev`):
//   g++ -std=c++17 -I/usr/include/libindi power_simulator.cpp -o indi_power_simulator -lindidriver
// Run it in its own INDI server, next to Ekos's:
//   indiserver -u /tmp/indiserver-power -p 7625 ./indi_power_simulator
//   indi_setprop -p 7625 "Power Simulator.CONNECTION.CONNECT=On"
//   indi_setprop -p 7625 "Power Simulator.SIMULATOR_SETTINGS.SIM_VOLTAGE=11.0"
#include <defaultdevice.h>
#include <indipowerinterface.h>
#include <cmath>
#include <memory>

class PowerSim : public INDI::DefaultDevice, public INDI::PowerInterface
{
  public:
    PowerSim() : INDI::PowerInterface(this)
    {
        SetCapability(POWER_HAS_DC_OUT | POWER_HAS_DEW_OUT | POWER_HAS_VOLTAGE_SENSOR |
                      POWER_HAS_OVERALL_CURRENT | POWER_HAS_PER_PORT_CURRENT | POWER_HAS_AUTO_DEW |
                      POWER_HAS_USB_TOGGLE);
    }
    const char *getDefaultName() override { return "Power Simulator"; }

    bool initProperties() override
    {
        INDI::DefaultDevice::initProperties();
        PI::initProperties("Power", 4, 2, 0, 2, 2);
        SimNP[0].fill("SIM_VOLTAGE", "Input (V)", "%.2f", 0, 20, 0.1, 12.6);
        SimNP[1].fill("SIM_TRIP", "Trip port (0=none)", "%.0f", 0, 4, 1, 0);
        SimNP.fill(getDeviceName(), "SIMULATOR_SETTINGS", "Simulator", "Simulator", IP_RW, 60, IPS_IDLE);
        setDriverInterface(AUX_INTERFACE | POWER_INTERFACE);
        addAuxControls();
        return true;
    }
    bool updateProperties() override
    {
        INDI::DefaultDevice::updateProperties();
        PI::updateProperties();
        if (isConnected()) { defineProperty(SimNP); SetTimer(1000); }
        else deleteProperty(SimNP);
        return true;
    }
    bool Connect() override { return true; }
    bool Disconnect() override { return true; }
    bool ISNewNumber(const char *dev, const char *name, double values[], char *names[], int n) override
    {
        if (dev && !strcmp(dev, getDeviceName()) && SimNP.isNameMatch(name))
        {
            SimNP.update(values, names, n);
            SimNP.setState(IPS_OK);
            SimNP.apply();
            return true;
        }
        if (PI::processNumber(dev, name, values, names, n)) return true;
        return INDI::DefaultDevice::ISNewNumber(dev, name, values, names, n);
    }
    bool ISNewSwitch(const char *dev, const char *name, ISState *states, char *names[], int n) override
    {
        if (PI::processSwitch(dev, name, states, names, n)) return true;
        return INDI::DefaultDevice::ISNewSwitch(dev, name, states, names, n);
    }
    bool ISNewText(const char *dev, const char *name, char *texts[], char *names[], int n) override
    {
        if (PI::processText(dev, name, texts, names, n)) return true;
        return INDI::DefaultDevice::ISNewText(dev, name, texts, names, n);
    }
    void TimerHit() override
    {
        if (!isConnected()) return;
        const double base[4] = {1.20, 0.45, 0.80, 0.30};
        int trip = (int)SimNP[1].getValue();
        double total = 0;
        for (int i = 0; i < 4; i++)
        {
            bool on = PowerChannelsSP[i].getState() == ISS_ON;
            if (trip == i + 1 && on) { PowerChannelsSP[i].setState(ISS_OFF); on = false; PowerChannelsSP.apply(); }
            double a = on ? base[i] + 0.02 * std::sin(tick + i) : 0;
            PowerChannelCurrentNP[i].setValue(a);
            total += a;
        }
        PowerChannelCurrentNP.setState(IPS_OK);
        PowerChannelCurrentNP.apply();
        for (int i = 0; i < 2; i++)
        {
            double a = DewChannelsSP[i].getState() == ISS_ON ? 0.03 * DewChannelDutyCycleNP[i].getValue() : 0;
            DewChannelCurrentNP[i].setValue(a);
            total += a;
        }
        DewChannelCurrentNP.apply();
        double v = SimNP[0].getValue();
        PowerSensorsNP[SENSOR_VOLTAGE].setValue(v);
        PowerSensorsNP[SENSOR_CURRENT].setValue(total);
        PowerSensorsNP[SENSOR_POWER].setValue(v * total);
        PowerSensorsNP.setState(IPS_OK);
        PowerSensorsNP.apply();
        tick++;
        SetTimer(1000);
    }
    bool saveConfigItems(FILE *fp) override
    {
        INDI::DefaultDevice::saveConfigItems(fp);
        PI::saveConfigItems(fp);
        return true;
    }

  private:
    INDI::PropertyNumber SimNP {2};
    int tick = 0;
};

static std::unique_ptr<PowerSim> sim(new PowerSim());
